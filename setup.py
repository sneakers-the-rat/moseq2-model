from setuptools import setup, find_packages

setup(
    name='kinect_modeling',
    version='0.0.1',
    author='Datta Lab',
    description='Modeling for the best',
    license='Crapl',
    packages=find_packages(exclude='docs'),
    platforms='any',
    install_requires=['scipy', 'h5py', 'numpy >= 1.11.0', 'click', 'pybasicbayes', 'pyhsmm', 'autoregressive', 'joblib', 'hdf5storage', 'ruamel.yaml'],
    entry_points={'console_scripts': ['kinect_model = kinect_modeling.cli:cli']},
)
