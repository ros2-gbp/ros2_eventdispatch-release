from setuptools import setup, find_packages

package_name = 'eventdispatch_python'

setup(
    name='eventdispatch',
    version='0.2.32',
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    description='Event Dispatch, a discrete time synchronizer',
    url='http://github.com/cyan-at/python-eventdispatch',
    author='Charlie Yan',
    author_email='cyanatg@gmail.com',
    license='Apache-2.0',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    maintainer='Charlie Yan',
    maintainer_email='cyanatg@gmail.com',
    packages=find_packages(exclude=['test']),
    entry_points=dict(
        console_scripts=[
            'eventdispatch_example_core=eventdispatch.example_core:main',
            'eventdispatch_cs_main=eventdispatch.composite_semaphore:main',
            'eventdispatch_example_cs=eventdispatch.example_cs:main',
            'eventdispatch_example_sim_mujoco=eventdispatch.example_sim_mujoco:main'
        ]
    )
)
