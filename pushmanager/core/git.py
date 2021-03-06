# -*- coding: utf-8 -*-
"""
Core Git Module

This module provides a GitQueue to pushmanager, into which three types of task
can be enqueued:
- Verify Branch: Check that a given branch exists
- Test Pickme Conflict: Check if a pickme conflicts with other pickmes in the
  same push
- Test All Pickmes: Recheck every pickme in a push against every other pickme in
  the push.

Notifications for verify failures and pickme conflicts are sent to the XMPP and
Mail queues.
"""
import functools
import logging
import os
import subprocess
import time
import urllib2
from multiprocessing import JoinableQueue
from multiprocessing import Process
from urllib import urlencode

from . import db
from .mail import MailQueue
from contextlib import contextmanager
from pushmanager.core.settings import Settings
from pushmanager.core.util import add_to_tags_str
from pushmanager.core.util import del_from_tags_str
from pushmanager.core.util import EscapedDict
from pushmanager.core.util import tags_contain
from pushmanager.core.xmppclient import XMPPQueue
from sqlalchemy import or_
from tornado.escape import xhtml_escape


@contextmanager
def git_branch_context_manager(test_branch, master_repo_path):
    """Context manager that creates / deletes a temporary git branch

    :param test_branch: The name of the temporary branch to create
    :param master_repo_path: The on-disk path to the master repository
    """

    # Remove the testing branch if it exists
    try:
        GitCommand("branch", "-D", test_branch, cwd=master_repo_path).run()
    except GitException:
        pass

    # Create a new branch tracking master
    make_test_branch = GitCommand(
        "checkout",
        "origin/master",
        "-b",
        test_branch,
        cwd=master_repo_path
    )
    make_test_branch.run()

    try:
        yield
    except Exception, e:
        raise e
    finally:
        # Checkout master so that we can delete the test branch
        checkout_master = GitCommand(
            'checkout',
            'master',
            cwd=master_repo_path
        )
        checkout_master.run()

        # Delete the branch that we were working on
        delete_test_branch = GitCommand(
            'branch',
            '-D',
            test_branch,
            cwd=master_repo_path
        )
        delete_test_branch.run()


def git_reset_to_ref(starting_ref, git_directory):
    """
    Resets a git repo to the specified ref.
    Called as a cleanup fn by git_merge_context_manager.

    :param starting_ref: Git hash of the commit to roll back to
    """

    GitCommand(
        'reset',
        '--hard',
        starting_ref,
        cwd=git_directory
    ).run()

    GitCommand(
        'submodule',
        '--quiet',
        'sync',
        cwd=git_directory
    ).run()

    GitCommand(
        'submodule',
        '--quiet',
        'update',
        cwd=git_directory
    ).run()


def _stale_submodule_check(cwd):
    """
    Checks that no submodules in the git repository path specified by cwd are
    out of date or too new.

    If any out of date submodules are found, update them.

    Once all submodules are up to date, calls _check_submodule on each
    changed submodule.
    :param cwd: On-disk path of the git repo to work with
    """

    stale_submodules = GitCommand('submodule', 'status', cwd=cwd)
    _, submodule_out, _ = stale_submodules.run()
    submodule_out = submodule_out.strip()

    # If nothing was returned, there are no submodules to check
    if len(submodule_out) == 0:
        return

    submodule_lines = submodule_out.split('\n')
    stale_submodules = []
    for submodule_line in submodule_lines:
        try:
            _, path, _ = submodule_line.strip().split(' ')
            if submodule_line[0] == '-' or submodule_line[0] == '+':
                stale_submodules.append(path)
        except ValueError:
            logging.error("Failed to unpack line %s", submodule_line)

    # If there are no stale submodules, nothing to do
    if len(stale_submodules) == 0:
        return

    logging.info("Submodules touched in this branch: %s",
                 ' '.join(stale_submodules))
    old_shas = GitCommand(
        'submodule', 'foreach', '--quiet',
        'echo "$path\t$(git rev-parse HEAD | cut -c-7)"',
        cwd=cwd
    )
    _, old_shas_out, _ = old_shas.run()
    old_shas_out = old_shas_out.strip()
    old_sha_list = old_shas_out.split('\n')

    GitCommand('submodule', '--quiet', 'sync', cwd=cwd).run()

    # Only fetch changed submodules
    for submodule in stale_submodules:
        GitCommand('submodule', 'update', '--init', submodule, cwd=cwd).run()
        GitCommand('--git-dir=%s/.git' % submodule, 'fetch', cwd=cwd).run()

    _check_submodule(cwd, stale_submodules, old_sha_list)


def _check_submodule(cwd, submodule_names, old_shas):
    """
    Checks that submodules
        - Have a master branch
        - Have been pushed to their master
        - if the local and remote version differ, ensure that they can be
          fast-forwarded.

    If any of these fail, raise a GitException with some details.

    :param cwd: On-disk path of the git repo to work with
    :param submodule_names: List of names (relative paths) of submodules to check
    :param old_shas: List of SHAs of the current versions of the submodules
    """

    for name in submodule_names:
        if _check_submodule_has_a_master(cwd, name):
            if not _check_submodule_head_is_in_master(cwd, name):
                exn_text = (
                    "Submodule error: %s has not been pushed to 'master'"
                    % name
                )
                raise GitException(
                    exn_text,
                    gitret=-1,
                    gitout=exn_text,
                    giterr=exn_text
                    )

        # Find the sha that corresponds to the outdated submodule
        old_sha = None
        for sha in old_shas:
            if sha.startswith(name):
                old_sha = sha.split('\t')[1]

        if not _check_submodule_is_fast_forward(cwd, name, old_sha):
            exn_text = (
                "Submodule Error: %s is not a fast forward of %s"
                % (name, old_sha)
            )
            raise GitException(
                exn_text,
                gitret=-1,
                gitout=exn_text,
                giterr=exn_text
            )


def _check_submodule_is_fast_forward(cwd, submodule_name, old_sha):
    submodule_path = os.path.join(cwd, submodule_name)
    _, new_sha, _ = GitCommand('rev-parse', 'HEAD', cwd=submodule_path).run()
    _, submodule_out, _ = GitCommand(
        'rev-list', '-n1', '%s..%s'
        % (new_sha.strip(), old_sha), cwd=submodule_path
    ).run()
    if len(submodule_out.strip()) > 0:
        return False
    return True


def _check_submodule_has_a_master(cwd, submodule_name):
    submodule_path = os.path.join(cwd, submodule_name)
    _, branch_output, _ = GitCommand('branch', '-r', cwd=submodule_path).run()
    if "origin/master" in branch_output:
        return True
    else:
        return False


def _check_submodule_head_is_in_master(cwd, submodule_name):
    submodule_path = os.path.join(cwd, submodule_name)

    _, head_sha, _ = GitCommand('rev-parse', 'HEAD', cwd=submodule_path).run()
    _, branch_output, _ = GitCommand(
        'branch', '-r', '--contains', head_sha.strip(),
        cwd=submodule_path
    ).run()

    return len(branch_output.strip()) > 0


@contextmanager
def git_merge_context_manager(test_branch, master_repo_path):
    """Context manager for merging that rolls back on __exit__

    :param test_branch: The name of the branch to merge onto
    :param master_repo_path: The on-disk path to the master repository
    """

    # Store the starting ref so that we can hard reset if need be
    get_starting_ref = GitCommand(
        'rev-parse',
        test_branch,
        cwd=master_repo_path
    )
    _, stdout, _ = get_starting_ref.run()

    starting_ref = stdout.strip()

    try:
        yield
    except Exception, e:
        raise e
    finally:
        git_reset_to_ref(
            starting_ref,
            master_repo_path
        )


class GitTaskAction(object):
    VERIFY_BRANCH = 1
    TEST_PICKME_CONFLICT = 2
    TEST_ALL_PICKMES = 3
    TEST_CONFLICTING_PICKMES = 4


class GitQueueTask(object):
    """
    A task for the GitQueue to perform.
    Task can be one of:
    - VERIFY_BRANCH: check that a branch can be found and is not a duplicate
    - TEST_PICKME_CONFLICT: check which (if any) branches also pickme'd for the
        same push cause merge conflicts with this branch
    - TEST_ALL_PICKMES: Takes a push id, and queues every pushme with a conflict-pickme tag
    - TEST_CONFLICTING_PICKMES. Used when an item is de-pickmed to ensure that
        anything it might have conlficted with is unmarked
    """

    def __init__(self, task_type, request_id, **kwargs):
        self.task_type = task_type
        self.request_id = request_id
        self.kwargs = kwargs


class GitException(Exception):
    """
    Exception class to be thrown in Git contexts
    Has fields for git output on top of  basic exception information.

    :param gitret: Return code from the failing Git process
    :param gitout: Stdout for the git process
    :param giterr: Stderr for the git process
    :param gitkwargs: Keyword arguments that were passed to the Git subprocess
    """
    def __init__(self, details, gitret=None, gitout=None,
                 giterr=None, gitkwargs=None):
        super(GitException, self).__init__(details, gitout, giterr, gitkwargs)
        self.details = details
        self.gitret = gitret
        self.gitout = gitout
        self.giterr = giterr
        self.gitkwargs = gitkwargs


class GitCommand(subprocess.Popen):

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _args = ['git'] + list(args)
        _kwargs = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
        }
        _kwargs.update(kwargs)
        subprocess.Popen.__init__(self, _args, **_kwargs)

    def run(self):
        stdout, stderr = self.communicate()
        if Settings['main_app']['debug']:
            logging.error("%r, %r, %r", self.args, stdout, stderr)
        if self.returncode:
            raise GitException(
                "GitException: git %s " % ' '.join(self.args),
                gitret=self.returncode,
                giterr=stderr,
                gitout=stdout,
                gitkwargs=self.kwargs
            )
        return self.returncode, stdout, stderr


class GitQueue(object):

    conflict_queue = None
    sha_queue = None
    conflict_worker_process = None
    sha_worker_process = None

    shas_in_master = {}

    EXCLUDE_FROM_GIT_VERIFICATION = Settings['git']['exclude_from_verification']

    @classmethod
    def request_is_excluded_from_git_verification(cls, request):
        """Some tags modify the workflow and are excluded from repository
        verification.
        """
        return tags_contain(request['tags'], cls.EXCLUDE_FROM_GIT_VERIFICATION)

    @classmethod
    def start_worker(cls):
        worker_pids = []
        if cls.conflict_worker_process is not None and cls.sha_worker_process is not None:
            return worker_pids

        cls.conflict_queue = JoinableQueue()
        cls.sha_queue = JoinableQueue()

        cls.conflict_workers = []
        for worker_id in range(Settings['git']['conflict-threads']):
            processing_func = functools.partial(
                cls.process_conflict_queue,
                worker_id
            )
            worker_thread = Process(target=processing_func, name='git-conflict-queue')
            worker_thread.daemon = True
            worker_thread.start()
            cls.conflict_workers.append(worker_thread)
            worker_pids.append(worker_thread.pid)

        cls.sha_worker_process = Process(target=cls.process_sha_queue, name='git-sha-queue')
        cls.sha_worker_process.daemon = True
        cls.sha_worker_process.start()
        worker_pids.append(cls.sha_worker_process.pid)

        cls.check_sha_worker_proc = Process(target=cls.check_active_request_shas, name='git-branch-sha-updater-daemon')
        cls.check_sha_worker_proc.daemon = True
        cls.check_sha_worker_proc.start()
        worker_pids.append(cls.check_sha_worker_proc.pid)

        return worker_pids

    @classmethod
    def git_merge_pickme(cls, worker_id, pickme_request, master_repo_path):
        """Merges the branch specified by a pickme onto the current branch

        :param pickme_request: Dictionary representing the pickme to merge
        :param master_repo_path: On-disk path of the git repo to work in
        """

        # Ensure that the branch we are merging is present
        cls.create_or_update_local_repo(
            worker_id,
            pickme_request['repo'],
            pickme_request['branch'],
            checkout=False
        )

        # Locate and merge the branch we are testing
        summary = "{branch_title}\n\n(Merged from {repo}/{branch})".format(
            branch_title=pickme_request['title'],
            repo=pickme_request['repo'],
            branch=pickme_request['branch']
        )

        pull_command = GitCommand(
            "pull",
            "--no-ff",
            "--no-commit",
            "--no-rebase",
            pickme_request['repo'],
            pickme_request['branch'],
            cwd=master_repo_path)
        pull_command.run()

        commit_command = GitCommand(
            "commit", "-m", summary,
            "--no-verify", cwd=master_repo_path
        )
        commit_command.run()

        # Verify that submodules are OK
        _stale_submodule_check(master_repo_path)

    @classmethod
    def create_or_update_local_repo(cls, worker_id, repo_name, branch, checkout=True, fetch=False):
        """Clones the main repository if it does not exist.
        If repo_name is not the main repo, add that repo as a remote and fetch
        refs before checking out the specified branch.
        """

        # Since we are keeping everything in the same repo, repo_path should
        # always be the same
        repo_path = cls._get_local_repository_uri(
            Settings['git']['main_repository'],
            worker_id
        )

        # repo_name is the remote to use. If we are dealing with the main
        # repository, set the remote to origin.
        if repo_name is Settings['git']['main_repository']:
            repo_name = 'origin'

        # Check if the main repo does not exist and needs to be created
        if not os.path.isdir(repo_path):
            # If we are using a reference mirror, add --reference [path] to
            # the list of gitcommand args
            clone_args = ['clone', cls._get_repository_uri(
                Settings['git']['main_repository']
            )]

            if Settings['git']['use_local_mirror']:
                if os.path.isdir(Settings['git']['local_mirror']):
                    clone_args.extend([
                        '--reference',
                        Settings['git']['local_mirror']
                    ])

            clone_args.append(repo_path)
            # Clone the main repo into repo_path. Will take time!
            clone_repo = GitCommand(*clone_args)
            clone_repo.run()

        if fetch:
            # If we are dealing with a dev repo, make sure it is added as a remote
            dev_repo_uri = cls._get_repository_uri(repo_name)
            add_remote = GitCommand(
                'remote', 'add', repo_name, dev_repo_uri,
                cwd=repo_path
            )
            try:
                add_remote.run()
            except GitException, e:
                # If the remote already exists, git will return err 128
                if e.gitret == 128:
                    pass
                else:
                    raise e

            # Fetch the specified branch from the repo
            remote_path = '+refs/heads/{branch}:refs/remotes/{repo}/{branch}'.format(
                branch=branch,
                repo=repo_name
            )

            fetch_updates = GitCommand(
                'fetch',
                '--prune',
                repo_name,
                remote_path,
                cwd=repo_path
            )
            fetch_updates.run()

        if checkout:
            # Reset hard head, to ensure that we are able to checkout
            GitCommand('reset', '--hard', 'HEAD', cwd=repo_path).run()

            # Remove untracked files and directories
            GitCommand('clean', '-fdfx', cwd=repo_path).run()

            # Checkout the branch
            full_branch = "%s/%s" % (repo_name, branch)
            checkout_branch = GitCommand('checkout', full_branch, cwd=repo_path)
            checkout_branch.run()

            # Update submodules
            sync_submodule = GitCommand(
                "submodule", "--quiet", "sync",
                cwd=repo_path
            )
            sync_submodule.run()
            update_submodules = GitCommand(
                "submodule", "--quiet", "update", "--init",
                cwd=repo_path
            )
            update_submodules.run()

    @classmethod
    def _get_local_repository_uri(cls, repository, worker_id):
        worker_repo = "{0}.{1}".format(repository, worker_id)
        return os.path.join(Settings['git']['local_repo_path'], worker_repo)

    @classmethod
    def _get_repository_uri(cls, repository):
        scheme = Settings['git']['scheme']
        netloc = Settings['git']['servername']
        if Settings['git']['auth']:
            netloc = '%s@%s' % (Settings['git']['auth'], netloc)
        if Settings['git']['port']:
            netloc = '%s:%s' % (netloc, Settings['git']['port'])
        if repository == Settings['git']['main_repository'] or repository == 'origin':
            repository = (
                '%s://%s/%s'
                % (scheme, netloc, Settings['git']['main_repository'])
            )
        else:
            repository = (
                '%s://%s/%s/%s' % (
                    scheme, netloc,
                    Settings['git']['dev_repositories_dir'],
                    repository
                )
            )
        return repository

    @classmethod
    def _get_branch_sha_from_repo(cls, req, alert=True):
        user_to_notify = req['user']
        query_details = {
            'user': req['user'],
            'title': req['title'],
            'repo': req['repo'],
            'branch': req['branch'],
        }
        stdout = ""
        try:
            ls_remote = GitCommand(
                'ls-remote', '-h',
                cls._get_repository_uri(req['repo']), req['branch']
            )
            _, stdout, _ = ls_remote.run()
            stdout = stdout.strip()
        except GitException, e:
            msg = """
                <p>
                    There was an error verifying your push request in Git:
                </p>
                <p>
                    <strong>%(user)s - %(title)s</strong><br />
                    <em>%(repo)s/%(branch)s</em>
                </p>
                <p>
                    Attempting to query the specified repository failed with
                    the following error(s):
                </p>
                <pre>
%(stderr)s
                </pre>
                <p>
                    Regards,<br/>
                    PushManager
                </p>
                """
            query_details['stderr'] = e.giterr
            msg %= EscapedDict(query_details)
            subject = '[push error] %s - %s' % (req['user'], req['title'])
            if alert:
                MailQueue.enqueue_user_email([user_to_notify], msg, subject)
            return None

        # successful ls-remote, build up the refs list
        tokens = (tok for tok in stdout.split())
        refs = zip(tokens, tokens)
        for sha, ref in refs:
            if ref == ('refs/heads/%s' % req['branch']):
                return sha

        msg = (
            """
            <p>
                There was an error verifying your push request in Git:
            </p>
            <p>
                <strong>%(user)s - %(title)s</strong><br />
                <em>%(repo)s/%(branch)s</em>
            </p>
            <p>
                The specified branch (%(branch)s) was not found in the
                repository.
            </p>
            <p>
                Regards,<br/>
                PushManager
            </p>
            """)
        msg %= EscapedDict(query_details)
        subject = '[push error] %s - %s' % (req['user'], req['title'])
        if alert:
            MailQueue.enqueue_user_email([user_to_notify], msg, subject)
        return None

    @classmethod
    def _get_request(cls, request_id):
        result = [None]

        def on_db_return(success, db_results):
            assert success, "Database error."
            result[0] = db_results.first()

        request_info_query = db.push_requests.select().where(
            db.push_requests.c.id == request_id
        )
        db.execute_cb(request_info_query, on_db_return)
        req = result[0]
        if req:
            req = dict(req.items())
        return req

    @classmethod
    def _get_request_ids_in_push(cls, push_id):
        """Return a list of IDs corresponding with the push requests
        that have been pickmed for the push specified by push_id

        :param push_id: Integer id of the push to get pickmes for
        :return pickme_ids: List of pickme IDs from the database
        """
        pickme_list = []

        def on_db_return(success, db_results):
            assert success, "Database error."
            for (request, _) in db_results:
                pickme_list.append(str(request))

        request_info_query = db.push_pushcontents.select().where(
            db.push_pushcontents.c.push == int(push_id)
        )
        db.execute_cb(request_info_query, on_db_return)
        return pickme_list

    @classmethod
    def _get_push_for_request(cls, request_id):
        """Given the ID of a push request, find the push for which this
        request has been pickmed.
        """
        result = [None]

        def on_db_return(success, db_results):
            assert success, "Database error."
            result[0] = db_results.first()

        request_info_query = db.push_pushcontents.select().where(
            db.push_pushcontents.c.request == request_id
        )
        db.execute_cb(request_info_query, on_db_return)
        req = result[0]
        if req:
            req = dict(req.items())
        return req

    @classmethod
    def _get_request_with_sha(cls, sha):
        result = [None]

        def on_db_return(success, db_results):
            assert success, "Database error."
            result[0] = db_results.first()

        request_info_query = db.push_requests.select().where(
            db.push_requests.c.revision == sha
        )
        db.execute_cb(request_info_query, on_db_return)
        req = result[0]
        if req:
            req = dict(req.items())
        return req

    @classmethod
    def _update_request(cls, req, updated_values):
        result = [None]

        def on_db_return(success, db_results):
            result[0] = db_results[1].first()
            assert success, "Database error."

        update_query = db.push_requests.update().where(
            db.push_requests.c.id == req['id']
        ).values(updated_values)
        select_query = db.push_requests.select().where(
            db.push_requests.c.id == req['id']
        )
        db.execute_transaction_cb([update_query, select_query], on_db_return)

        updated_request = result[0]
        if updated_request:
            updated_request = dict(updated_request.items())
        if not updated_request:
            logging.error(
                "Git-queue worker failed to update the request (id %s).",
                req['id']
            )
            logging.error(
                "Updated Request values were: %s",
                repr(updated_values)
            )

        return updated_request

    @classmethod
    def _sha_exists_in_master(cls, worker_id, sha):
        """Check if a given SHA is included in master
        Memoize shas that are, so that we can avoid expensive rev-lists later.
        We can't cache shas that are not in master, since we won't know when they get merged.
        """

        # Dirty cache expiry mechanism, but better than constantly
        # accumulating SHAs in memory
        if len(cls.shas_in_master) > 1000:
            cls.shas_in_master = {}

        if sha in cls.shas_in_master:
            return True

        repo_path = cls._get_local_repository_uri(
            Settings['git']['main_repository'],
            worker_id
        )

        try:
            _, merge_base, _ = GitCommand('merge-base', 'origin/master', sha, cwd=repo_path).run()
        except GitException:
            # If the hash is entirely unknown, Git will throw an error
            # fatal: Not a valid commit name <sha>.
            return False

        merge_base = merge_base.strip()

        if sha == merge_base:
            cls.shas_in_master[sha] = True
            return True
        else:
            return False

    @classmethod
    def _test_pickme_conflict_pickme(cls, worker_id, req, target_branch,
                                     repo_path, pushmanager_url, requeue):
        """Test for any pickmes that are broken by pickme'd request req

        Precondition: We should already be on a test branch, and the pickme to
        be tested against should already be successfully merged.

        :param req: Details for pickme to test against
        :param target_branch: Name of branch onto which to attempt merge
        :param repo_path: On-disk path to local repository
        :param requeue: Boolean whether or not to requeue pickmes that are conflicted with
        """

        push = cls._get_push_for_request(req['id'])
        if push is None:
            logging.warn(
                "Couldn't test pickme %d - couldn't find corresponding push",
                req['id']
            )
            return False, None

        pickme_ids = cls._get_request_ids_in_push(push['push'])

        pickme_ids = [p for p in pickme_ids if int(p) != int(req['id'])]

        conflict_pickmes = []

        # For each pickme, check if merging it on top throws an exception.
        # If it does, keep track of the pickme in conflict_pickmes
        for pickme in pickme_ids:
            pickme_details = cls._get_request(pickme)
            if not pickme_details:
                logging.error(
                    "Tried to test for conflicts against invalid request id %s",
                    pickme
                )
                continue

            if 'state' not in pickme_details or pickme_details['state'] not in ('pickme', 'added'):
                continue

            # Ensure we have a copy of the pickme we are comparing against
            cls.create_or_update_local_repo(
                worker_id,
                pickme_details['repo'],
                branch=pickme_details['branch'],
                fetch=True,
                checkout=False
            )

            # Don't check against pickmes that are already in master, as
            # it would throw 'nothing to commit' errors
            sha = cls._get_branch_sha_from_repo(pickme_details)
            if sha is None or cls._sha_exists_in_master(worker_id, sha):
                continue

            # If the pickme has no '*conflict*' tags, it has not been checked and
            # it may conflict with master, which here would cause a pickme
            # conflict. Skip it, as it should be queued to be checked, and will
            # get tested against us later.
            if "conflict" not in pickme_details['tags']:
                continue

            # Don't bother trying to compare against pickmes that
            # break master, as they will conflict by default
            if "conflict-master" in pickme_details['tags']:
                continue

            try:
                with git_merge_context_manager(target_branch,
                                               repo_path):
                    cls.git_merge_pickme(worker_id, pickme_details, repo_path)
            except GitException, e:
                if req['state'] == 'added' and pickme_details['state'] == 'pickme':
                    pass
                else:
                    conflict_pickmes.append((pickme, e.gitout, e.giterr))
                # Requeue the conflicting pickme so that it also picks up the
                # conflict. Pass on that it was requeued automatically and to
                # NOT requeue things in that run, otherwise two tickets will
                # requeue each other forever.
                if requeue and pickme_details['state'] != 'added':
                    GitQueue.enqueue_request(
                        GitTaskAction.TEST_PICKME_CONFLICT,
                        pickme,
                        pushmanager_url=pushmanager_url,
                        requeue=False
                    )

        # If there were no conflicts, don't update the request
        if not conflict_pickmes:
            return False, None

        updated_tags = add_to_tags_str(req['tags'], 'conflict-pickme')
        updated_tags = del_from_tags_str(updated_tags, 'no-conflicts')
        formatted_conflicts = ""
        for broken_pickme, git_out, git_err in conflict_pickmes:
            pickme_details = cls._get_request(broken_pickme)
            formatted_pickme_err = (
                """<strong>Conflict with <a href=\"/request?id={pickme_id}\">
                {pickme_name}</a>: </strong><br/>{pickme_out}<br/>{pickme_err}
                <br/><br/>"""
            ).format(
                pickme_id=broken_pickme,
                pickme_err=xhtml_escape(git_err),
                pickme_out=xhtml_escape(git_out),
                pickme_name=xhtml_escape(pickme_details['title'])
            )
            formatted_conflicts += formatted_pickme_err

        updated_values = {
            'tags': updated_tags,
            'conflicts': formatted_conflicts
        }

        updated_request = cls._update_request(req, updated_values)
        if not updated_request:
            raise Exception("Failed to update pickme details")
        else:
            return True, updated_request

    @classmethod
    def _clear_pickme_conflict_details(cls, req):
        """Strips the conflict-pickme, conflict-master and no-conflicts tags from a
        pickme, and clears the detailed conflict field.

        :param req: Details of pickme request to clear conflict details of
        """
        updated_tags = del_from_tags_str(req['tags'], 'conflict-master')
        updated_tags = del_from_tags_str(updated_tags, 'conflict-pickme')
        updated_tags = del_from_tags_str(updated_tags, 'no-conflicts')
        updated_values = {
            'tags': updated_tags,
            'conflicts': ''
        }
        updated_request = cls._update_request(req, updated_values)
        if not updated_request:
            raise Exception("Failed to update pickme")

    @classmethod
    def _test_pickme_conflict_master(
            cls, worker_id, req, target_branch,
            repo_path, pushmanager_url, requeue):
        """Test whether the pickme given by req can be successfully merged onto
        master.

        If the pickme was merged successfully, it calls
        _test_pickme_conflict_pickme to check the pickme against others in the
        same push.

        :param req: Details of pickme request to test
        :param target_branch: The name of the test branch to use for testing
        :param repo_path: The location of the repository we are working in
        """

        # Ensure we have a copy of the pickme branch
        cls.create_or_update_local_repo(
            worker_id,
            req['repo'],
            branch=req['branch'],
            fetch=True,
            checkout=False
        )

        # Create a test branch following master
        with git_branch_context_manager(target_branch, repo_path):
            # Merge the pickme we are testing onto the test branch
            # If this fails, that means pickme conflicts with master
            try:
                with git_merge_context_manager(target_branch, repo_path):
                    # Try to merge the pickme onto master
                    cls.git_merge_pickme(worker_id, req, repo_path)

                    # Check for conflicts with other pickmes
                    return cls._test_pickme_conflict_pickme(
                        worker_id,
                        req,
                        target_branch,
                        repo_path,
                        pushmanager_url,
                        requeue
                    )

            except GitException, e:
                updated_tags = add_to_tags_str(req['tags'], 'conflict-master')
                updated_tags = del_from_tags_str(updated_tags, 'no-conflicts')
                conflict_details = "<strong>Conflict with master:</strong><br/> %s <br/> %s" % (e.gitout, e.giterr)
                updated_values = {
                    'tags': updated_tags,
                    'conflicts': conflict_details
                }

                updated_request = cls._update_request(req, updated_values)
                if not updated_request:
                    raise Exception("Failed to update pickme")
                else:
                    return True, updated_request

    @classmethod
    def test_pickme_conflicts(
            cls,
            worker_id,
            request_id,
            pushmanager_url,
            requeue=True):
        """
        Tests for conflicts between a pickme and both master and other pickmes
        in the same push.

        :param request_id: ID number of the pickme to be tested
        :param requeue: Whether or not pickmes that this pickme conflicts with
            should be added back into the GitQueue as a test conflict task.
        """

        req = cls._get_request(request_id)
        if not req:
            logging.error(
                "Tried to test conflicts for invalid request id %s",
                request_id
            )
            return

        if 'state' not in req or req['state'] not in ('pickme', 'added'):
            return

        push = cls._get_push_for_request(request_id)
        if not push:
            logging.error(
                "Request %s (%s) doesn't seem to be part of a push",
                request_id,
                req['title']
            )
            return
        push_id = push['push']

        # Set up the environment as though we are preparing a deploy push
        # Create a branch pickme_test_PUSHID_PICKMEID

        # Ensure that the local copy of master is up-to-date
        cls.create_or_update_local_repo(
            worker_id,
            Settings['git']['main_repository'],
            branch="master",
            fetch=True
        )

        # Get base paths and names for the relevant repos
        repo_path = cls._get_local_repository_uri(
            Settings['git']['main_repository'],
            worker_id
        )
        target_branch = "pickme_test_{push_id}_{pickme_id}".format(
            push_id=push_id,
            pickme_id=request_id
        )

        # Check that the branch is still reachable
        sha = cls._get_branch_sha_from_repo(req)
        if sha is None:
            return

        # Check if the pickme has already been merged into master
        if cls._sha_exists_in_master(worker_id, sha):
            return

        # Clear the pickme's conflict info
        cls._clear_pickme_conflict_details(req)

        # Check for conflicts with master
        conflict, updated_pickme = cls._test_pickme_conflict_master(
            worker_id,
            req,
            target_branch,
            repo_path,
            pushmanager_url,
            requeue
        )
        if conflict:
            if updated_pickme is None:
                raise Exception(
                    "Encountered merge conflict but was not passed details"
                )
            cls.pickme_conflict_detected(updated_pickme, requeue, pushmanager_url)
        else:
            # If the request does not conflict here or anywhere else, mark it as
            # no-conflicts
            req = cls._get_request(request_id)
            if 'conflict' in req['tags']:
                return
            updated_tags = add_to_tags_str(req['tags'], 'no-conflicts')
            updated_values = {
                'tags': updated_tags,
            }
            updated_request = cls._update_request(req, updated_values)
            if not updated_request:
                raise Exception("Failed to update pickme")

    @classmethod
    def pickme_conflict_detected(cls, updated_request, send_notifications, pushmanager_url):
        msg = (
            """
            <p>
                PushManager has detected that your pickme contains conflicts with %(conflicts_with)s.
            </p>
            <p>
                <strong>%(user)s - %(title)s</strong><br />
                <em>%(repo)s/%(branch)s</em><br />
                <a href="%(pushmanager_url)s/request?id=%(id)s">%(pushmanager_url)s/request?id=%(id)s</a>
            </p>
            <p>
                Review # (if specified): <a href="https://%(reviewboard_servername)s/r/%(reviewid)s">%(reviewid)s</a>
            </p>
            <p>
                <code>%(revision)s</code><br/>
                <em>(If this is <strong>not</strong> the revision you expected,
                make sure you've pushed your latest version to the correct repo!)</em>
            </p>
            <p>
                %(conflicts)s
            </p>
            <p>
                Regards,<br/>
                PushManager
            </p>
            """
        )
        updated_request.update(
            {
                'conflicts_with': (
                    "master"
                    if 'conflict-master' in updated_request['tags']
                    else "another pickme"
                ),
                'conflicts': updated_request['conflicts'].replace('\n', '<br/>'),
                'reviewboard_servername': Settings['reviewboard']['servername'],
                'pushmanager_url': pushmanager_url
            }
        )
        escaped_request = EscapedDict(updated_request)
        escaped_request.unescape_key('conflicts')
        msg %= escaped_request
        subject = (
            '[push conflict] %s - %s'
            % (updated_request['user'], updated_request['title'])
        )
        user_to_notify = updated_request['user']
        MailQueue.enqueue_user_email([user_to_notify], msg, subject)

        msg = """PushManager has detected that your pickme for %(pickme_name)s contains conflicts with %(conflicts_with)s
            %(pushmanager_url)s/request?id=%(pickme_id)s""" % {
                'conflicts_with': (
                    "master"
                    if 'conflict-master' in updated_request['tags']
                    else "another pickme"
                ),
                'pickme_name': updated_request['branch'],
                'pickme_id': updated_request['id'],
                'pushmanager_url': pushmanager_url
            }
        XMPPQueue.enqueue_user_xmpp([user_to_notify], msg)

    @classmethod
    def verify_branch(cls, request_id, pushmanager_url):
        req = cls._get_request(request_id)
        if not req:
            # Just log this and return. We won't be able to get more
            # data out of the request.
            error_msg = "Git queue worker received a job for non-existent request id %s" % request_id
            logging.error(error_msg)
            return

        if cls.request_is_excluded_from_git_verification(req):
            return

        if not req['branch']:
            error_msg = "Git queue worker received a job for request with no branch (id %s)" % request_id
            return cls.verify_branch_failure(req, error_msg, pushmanager_url)

        sha = cls._get_branch_sha_from_repo(req)
        if sha is None:
            error_msg = "Git queue worker could not get the revision from request branch (id %s)" % request_id
            return cls.verify_branch_failure(req, error_msg, pushmanager_url)

        duplicate_req = cls._get_request_with_sha(sha)
        if (
            duplicate_req and
            'state' in duplicate_req and
            not duplicate_req['state'] == "discarded" and
            duplicate_req['id'] != req['id']
        ):
            error_msg = "Git queue worker found another request with the same revision sha (ids %s and %s)" % (
                duplicate_req['id'],
                req['id']
            )
            return cls.verify_branch_failure(req, error_msg, pushmanager_url)

        updated_tags = add_to_tags_str(req['tags'], 'git-ok')
        updated_tags = del_from_tags_str(updated_tags, 'git-error')
        updated_values = {'revision': sha, 'tags': updated_tags}

        updated_request = cls._update_request(req, updated_values)
        if updated_request:
            cls.verify_branch_successful(updated_request, pushmanager_url)

    @classmethod
    def verify_branch_successful(cls, updated_request, pushmanager_url):
        msg = (
            """
            <p>
                PushManager has verified the branch for your request.
            </p>
            <p>
                <strong>%(user)s - %(title)s</strong><br />
                <em>%(repo)s/%(branch)s</em><br />
                <a href="%(pushmanager_url)s/request?id=%(id)s">%(pushmanager_url)s/request?id=%(id)s</a>
            </p>
            <p>
                Review # (if specified): <a href="https://%(reviewboard_servername)s/r/%(reviewid)s">%(reviewid)s</a>
            </p>
            <p>
                Verified revision: <code>%(revision)s</code><br/>
                <em>(If this is <strong>not</strong> the revision you expected,
                make sure you've pushed your latest version to the correct repo!)</em>
            </p>
            <p>
                Regards,<br/>
                PushManager
            </p>
            """
        )
        updated_request.update({
            'pushmanager_url': pushmanager_url,
            'reviewboard_servername': Settings['reviewboard']['servername']
        })
        msg %= EscapedDict(updated_request)
        subject = '[push] %s - %s' % (
            updated_request['user'],
            updated_request['title']
        )
        user_to_notify = updated_request['user']
        MailQueue.enqueue_user_email([user_to_notify], msg, subject)

        webhook_req(
            'pushrequest',
            updated_request['id'],
            'ref',
            updated_request['branch'],
        )

        webhook_req(
            'pushrequest',
            updated_request['id'],
            'commit',
            updated_request['revision'],
        )

        if updated_request['reviewid']:
            webhook_req(
                'pushrequest',
                updated_request['id'],
                'review',
                updated_request['reviewid'],
            )

    @classmethod
    def verify_branch_failure(cls, request, failure_msg, pushmanager_url):
        logging.error(failure_msg)
        updated_tags = add_to_tags_str(request['tags'], 'git-error')
        updated_tags = del_from_tags_str(updated_tags, 'git-ok')
        updated_values = {'tags': updated_tags}

        cls._update_request(request, updated_values)

        msg = (
            """
            <p>
                <em>PushManager could <strong>not</strong> verify the branch for your request.</em>
            </p>
            <p>
                <strong>%(user)s - %(title)s</strong><br />
                <em>%(repo)s/%(branch)s</em><br />
                <a href="%(pushmanager_url)s/request?id=%(id)s">%(pushmanager_url)s/request?id=%(id)s</a>
            </p>
            <p>
                <strong>Error message</strong>:<br />
                %(failure_msg)s
            </p>
            <p>
                Review # (if specified): <a href="https://%(reviewboard_servername)s/r/%(reviewid)s">%(reviewid)s</a>
            </p>
            <p>
                Verified revision: <code>%(revision)s</code><br/>
                <em>(If this is <strong>not</strong> the revision you expected,
                make sure you've pushed your latest version to the correct repo!)</em>
            </p>
            <p>
                Regards,<br/>
                PushManager
            </p>
            """
        )
        request.update({
            'failure_msg': failure_msg,
            'pushmanager_url': pushmanager_url,
            'reviewboard_servername': Settings['reviewboard']['servername']
        })
        msg %= EscapedDict(request)
        subject = '[push] %s - %s' % (request['user'], request['title'])
        user_to_notify = request['user']
        MailQueue.enqueue_user_email([user_to_notify], msg, subject)

    @classmethod
    def requeue_pickmes_for_push(cls, push_id, pushmanager_url, conflicting_only=False):
        request_details = []
        for pickme_id in cls._get_request_ids_in_push(push_id):
            request_details.append(cls._get_request(pickme_id))

        if conflicting_only:
            request_details = [
                req for req in request_details
                if req and req['tags'] and 'conflict-pickme' in req['tags']
            ]

        for req in request_details:
            GitQueue.enqueue_request(
                GitTaskAction.TEST_PICKME_CONFLICT,
                req['id'],
                pushmanager_url=pushmanager_url,
                requeue=False
            )

    @classmethod
    def _notify_updated_request_sha(cls, updated_req, new_sha):
        msg = """
                <p>
                    Your open request for the merging of branch %(branch)s has been updated
                </p>
                <p>
                    <strong>%(user)s - %(title)s</strong><br />
                    <em>%(repo)s/%(branch)s</em>
                </p>
                <p>
                    Old SHA of branch's head: %(revision)s<br/>
                    New SHA of branch's head: %(new_sha)s
                </p>
                <p>
                    Regards,<br/>
                    PushManager
                </p>
                """
        repl_dict = {
                'branch': updated_req['branch'],
                'user': updated_req['user'],
                'title': updated_req['title'],
                'repo': updated_req['repo'],
                'revision': updated_req['revision'],
                'new_sha': new_sha
                }
        msg %= repl_dict
        subject = '[push] %s - %s' % (updated_req['user'], updated_req['title'])
        user_to_notify = updated_req['user']
        MailQueue.enqueue_user_email([user_to_notify], msg, subject)
        return

    @classmethod
    def _get_active_requests(cls):
        ''' Returns any 'active' meaning any request that is still a live
        branch that has not been merged into any deploy or production branches.

        Request states that currently fall under this label are 'requested', 'pickme',
        and 'added' states. Any of these are 'active' and possibly subject to more
        change before they've been merged.
        '''
        result = [None]

        def on_db_return(success, db_results):
            assert success, "Database error."
            result[0] = db_results.fetchall()
            db_results.close()

        req_active_query = db.push_requests.select(or_(
            db.push_requests.c.state == 'requested',
            db.push_requests.c.state == 'pickme',
            db.push_requests.c.state == 'added')
        )

        db.execute_cb(req_active_query, on_db_return)
        reqs = result[0]
        return reqs

    @classmethod
    def process_sha_queue(cls):
        logging.info("Starting GitConflictQueue")
        while True:
            # Throttle
            time.sleep(1)

            task = cls.sha_queue.get()

            if not isinstance(task, GitQueueTask):
                logging.error("Non-task object in GitSHAQueue: %s", task)
                continue

            try:
                if task.task_type is GitTaskAction.VERIFY_BRANCH:
                    cls.verify_branch(task.request_id, task.kwargs['pushmanager_url'])
                else:
                    logging.error(
                        "GitSHAQueue encountered unknown task type %d",
                        task.task_type
                    )
            except Exception:
                logging.error('THREAD ERROR:', exc_info=True)
            finally:
                cls.sha_queue.task_done()

    @classmethod
    def process_conflict_queue(cls, worker_id):
        logging.error("Starting GitConflictQueue %d", worker_id)
        while True:
            # Throttle
            time.sleep(1)

            task = cls.conflict_queue.get()

            if not isinstance(task, GitQueueTask):
                logging.error("Non-task object in GitConflictQueue: %s", task)
                continue

            try:
                if task.task_type is GitTaskAction.TEST_PICKME_CONFLICT:
                    cls.test_pickme_conflicts(worker_id, task.request_id, **task.kwargs)
                elif task.task_type is GitTaskAction.TEST_CONFLICTING_PICKMES:
                    cls.requeue_pickmes_for_push(task.request_id, task.kwargs['pushmanager_url'], conflicting_only=True)
                elif task.task_type is GitTaskAction.TEST_ALL_PICKMES:
                    cls.requeue_pickmes_for_push(task.request_id, task.kwargs['pushmanager_url'])
                else:
                    logging.error(
                        "GitConflictQueue encountered unknown task type %d",
                        task.task_type
                    )
            except Exception:
                logging.error('THREAD ERROR:', exc_info=True)
            finally:
                cls.conflict_queue.task_done()

    @classmethod
    def check_active_request_shas(cls):
        '''Process daemon function that will continually poll the HEAD
        of a branch requested for inclusion in a push, update the request
        with the newest sha for that branch, and alert the user of the change.

        Will re-run conflict checks for requests in the pickme or added state to
        verify that updates to this branch will not include any new conflicts. And
        clears conflict tags if conflicts have been resolved.
        '''

        logging.info("Starting GitCheckActiveRequestSHADaemon")
        while True:
            time.sleep(1)  # Throttle a bit
            active_requests = cls._get_active_requests()

            if active_requests is None:
                continue

            for req in active_requests:
                time.sleep(.04)  # Try not to hammer the git repo
                sha = cls._get_branch_sha_from_repo(req, alert=False)
                if sha is None:
                    sha = '0'*40

                if cls.request_is_excluded_from_git_verification(req):
                    continue
                if not req['branch'] or not req['revision'] or sha == req['revision']:
                    continue
                try:
                    cls._update_req_sha_and_queue_pickme(req, sha)
                    cls._notify_updated_request_sha(req, sha)
                except Exception as e:
                    logging.error('THREAD ERROR: %s' % (e))

    @classmethod
    def _update_req_sha_and_queue_pickme(cls, req, sha):
        ''' Update request with new sha and re-run conflict checks if
        request is in pickme or added state.

        Args:
            req (dict): request to be updated
            sha (string): sha to update req['revision'] to

        Raises:
            Exception if fails to update request in DB
        '''
        logging.info("Updating: %s request's sha from %s to %s" % (req['title'], req['revision'], sha))
        updated_sha = {'revision': sha}
        updated_request = cls._update_request(req, updated_sha)
        if not updated_request:
            raise Exception("Failed to update pickme"
                            "%s request's sha from %s to %s" % (req['title'], req['revision'], sha))
        # TODO: No way to use proxy URL in daemon. Make URL prettier eventually
        raw_url = 'https://%s:%s' % (Settings['main_app']['servername'], Settings['main_app']['port'])
        GitQueue.enqueue_request(
            GitTaskAction.VERIFY_BRANCH,
            req['id'],
            pushmanager_url=raw_url
        )

        if req['state'] in ('pickme', 'added'):
            if 'no-conflicts' in req['tags'] or 'conflict-master' in req['tags']:
                # Only run conflict checks on this branch, since any other affected by it will be new
                # conflict-pickmes and caught normally
                GitQueue.enqueue_request(
                    GitTaskAction.TEST_PICKME_CONFLICT,
                    req['id'],
                    pushmanager_url=raw_url
                )
            elif 'conflict-pickme' in req['tags']:
                # Run on all conflict checks on all conflict-pickmes since this might resolve
                # conflicts between this branch and others
                GitQueue.enqueue_request(
                    GitTaskAction.TEST_CONFLICTING_PICKMES,
                    cls._get_push_for_request(req['id'])['push'],
                    pushmanager_url=raw_url
                )

    @classmethod
    def enqueue_request(cls, task_type, request_id, **kwargs):
        if task_type is GitTaskAction.VERIFY_BRANCH:
            if not cls.sha_queue:
                logging.error("Attempted to put to nonexistent GitSHAQueue!")
                return
            cls.sha_queue.put(GitQueueTask(task_type, request_id, **kwargs))
        else:
            if not cls.conflict_queue:
                logging.error("Attempted to put to nonexistent GitConflictQueue!")
                return
            cls.conflict_queue.put(GitQueueTask(task_type, request_id, **kwargs))


def webhook_req(left_type, left_token, right_type, right_token):
    webhook_url = Settings['web_hooks']['post_url']
    body = urlencode({
        'reason': 'pushmanager',
        'left_type': left_type,
        'left_token': left_token,
        'right_type': right_type,
        'right_token': right_token,
    })
    try:
        f = urllib2.urlopen(webhook_url, body, timeout=3)
        f.close()
    except urllib2.URLError:
        logging.error("Web hook POST failed:", exc_info=True)


__all__ = ['GitQueue']
