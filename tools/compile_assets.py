# -*- coding: utf-8 -*-
import os
import scss


project_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
static_root = os.path.join(project_path, 'pushmanager/static')

# STATIC_ROOT is where pyScss looks for images and static data.
# STATIC_ROOT can be either a fully qualified path name or a "finder"
# iterable function that receives a filename or glob and returns a tuple
# of the file found and its file storage object for each matching file.
# (https://docs.djangoproject.com/en/dev/ref/files/storage/)
scss.config.STATIC_ROOT = static_root

# These are the paths pyScss will look ".scss" files on. This can be the path to
# the compass framework or blueprint or compass-recepies, etc.
scss.config.LOAD_PATHS = [
    os.path.join(project_path, 'bower_components'),
]

# This creates the Scss object used to compile SCSS code. In this example,
# _scss_vars will hold the context variables:
_scss_vars = {}
_scss = scss.Scss(
    scss_vars=_scss_vars,
    scss_opts={
        'style': 'compressed',
        'debug_info': False,
    }
)


with open(os.path.join(static_root, 'css/application.css'), 'w') as f:
    f.write(
        _scss.compile(
            scss_file=os.path.join(static_root, 'scss/application.scss'),
        )
    )
