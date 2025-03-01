import glob

import os
import sys
from setuptools import setup, find_packages

README = open(os.path.join(os.path.dirname(__file__), 'README.rst')).read()
version = open(os.path.join(os.path.dirname(__file__), 'omegaml', 'VERSION')).read()

# extras
tables = ['tables>=3.7']
graph_deps = ['matplotlib~=3.5', 'seaborn~=0.11', 'imageio~=2.6', 'plotext~=1.0']
dashserve_deps = ['dashserve', 'dash<2.9'] # dash 2.9 breaks dashserve due to required pages folder
sql_deps = ['sqlalchemy', 'ipython-sql']
snowflake_deps = ['snowflake-sqlalchemy>1.2.3']
iotools_deps = ['boto>=2.49.0']
streaming_deps = ['minibatch[all]>=0.5.0']
jupyter_deps = ['jupyterlab', 'jupyterhub==2.2.1']
mlflow_deps = ['mlflow~=1.21']
dev_deps = ['pytest', 'twine', 'flake8', 'mock', 'behave', 'splinter[selenium3]', 'ipdb', 'bumpversion']

# -- tensorflow specifics
#    see https://www.tensorflow.org/install/source
tf_version = os.environ.get('TF_VERSION') or '2.3.1'
tf_match = os.environ.get('TF_VERSION_MATCH', '==')
if tf_version.startswith('1.15'):
    assert sys.version_info[:2] <= (3, 7), "TF < 2.x requires Python <= 3.7"
    tf_deps = ['tensorflow=={}'.format(tf_version)]
    tf_deps = tf_deps + ['tensorflow-gpu==1.15.0', 'h5py==2.10.0']
    keras_deps = ['keras==2.2.4']
elif (3, 8) <= sys.version_info[:2] < (3, 9):
    major, minor, *_ = (int(v) for v in tf_version.split('.'))
    assert (major, minor) >= (2, 2), "Python version 3.8 only supported by TF >= 2.2"
    tf_deps = ['tensorflow{}{}'.format(tf_match, tf_version)]
    keras_deps = ['keras~=2.4.3']
elif sys.version_info[:2] >= (3, 9):
    major, minor, *_ = (int(v) for v in tf_version.split('.'))
    tf_issue = "https://github.com/tensorflow/tensorflow/issues/44485"
    tf_deps = ['tensorflow>=2.5,<2.9']
    keras_deps = ['keras~=2.4.3']
else:
    # python 3.6, tensorflow 2.3.1 only supported with keras<=2.5
    tf_deps = ['tensorflow{}{}'.format(tf_match, tf_version)]
    keras_deps = ['keras<=2.5']

# all deps
all_deps = (tables + graph_deps + dashserve_deps + sql_deps + iotools_deps
            + streaming_deps + jupyter_deps + snowflake_deps)
client_deps = (tables + dashserve_deps + sql_deps + iotools_deps + streaming_deps)

setup(
    name='omegaml',
    version=version,
    packages=find_packages(),
    include_package_data=True,
    data_files=[
        ('omegaml/docs', glob.glob('./docs/source/nb/*.ipynb')),
        ('omegaml/runtimes/rsystem', glob.glob('./runtimes/rsystem/*.R')),
    ],
    scripts=glob.glob('./scripts/runtime/*'),
    license='Apache 2.0 + "No Sell, Consulting Yes" License Condition',
    description='An open source DataOps, MLOps platform for humans',
    long_description=README,
    long_description_content_type='text/x-rst',
    url='https://omegaml.io/',
    author='Patrick Senti',
    author_email='patrick.senti@omegaml.io',
    classifiers=[
        'License :: OSI Approved :: Apache Software License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: Implementation :: CPython',
        'Development Status :: 4 - Beta',
        'Topic :: Scientific/Engineering :: Artificial Intelligence',
        'Topic :: Software Development',
        'Operating System :: POSIX :: Linux',
        'License :: OSI Approved :: Apache Software License',
    ],
    install_requires=[
        'celery>5,<6.0',
        'importlib-metadata<5.0',  # due to https://github.com/celery/kombu/pull/1601
        'joblib>=0.9.4',
        'jupyter-client>=4.1.1',
        'mongoengine~=0.24.1',
        'pymongo~=4.0.2',  # mongoengine 0.24.1 compatibility
        'pandas>1.1,<2', # pandas 2.0 breaks some tests
        'numpy>=1.16.4',
        'scipy>=0.17.0',
        'scikit-learn>=0.21',
        'PyYAML>=3.12',
        'flask-restx>=0.4.0',
        'werkzeug<2.1.0',  # due to flask-restx, https://github.com/python-restx/flask-restx/issues/422
        'markupsafe==2.0.1',  # due to flask/markupsafe, https://github.com/pallets/markupsafe/issues/284
        'croniter>=0.3.30',
        'nbformat>=4.0.1',
        'nbconvert>=6.4.0',
        'pygments>=2.7.4',  # via nbconvert, due to snyk report https://github.com/omegaml/omegaml/pull/299
        'dill>=0.3.2,<0.3.6',  # due to dill, https://github.com/uqfoundation/dill/issues/332
        'callable-pip>=1.0.0',
        'appdirs>=1.4.3',
        'cron-descriptor>=1.2.31',
        'docopt>=0.6.2',
        'requests>=2.20.0',
        # fix tensorflow pulling wrong version of absl-py,
        # https://github.com/tensorflow/tensorflow/issues/26691#issuecomment-525519742
        'absl-py>=0.8.1',
        'tqdm>=4.32.2',
        'honcho>=1.0.1',  # not strictly required, but used in docker compose
        'tabulate>=0.8.2',  # required in cli
        'smart_open',  # required in cli
        'imageio>=2.3.0',  # require to store images
        'psutil>=5.8',  # required for profiling tracker
        'cachetools>=5.0.0',  # required for session caching
        'apispec>=5.2.2',  # required for openapi generation
        'marshmallow>=3.17.0',  # required for openapi generation
    ],
    extras_require={
        'graph': graph_deps,
        'tables': tables,
        'tensorflow': tf_deps,
        'keras': keras_deps,
        'jupyter': jupyter_deps,
        'dashserve': dashserve_deps,
        'sql': sql_deps,
        'snowflake': snowflake_deps,
        'mlflow': mlflow_deps,
        'iotools': iotools_deps,
        'streaming': streaming_deps,
        'all': all_deps,
        'client': client_deps,
        'all-client': client_deps,
        'dev': dev_deps,
    },
    entry_points={
        'console_scripts': ['om=omegaml.client.cli:climain'],
    }
)
