from sys import version_info

invalid_python_versions = [
    '2.7',
    '3.6',
    '3.7',
    '3.8',
    '3.9',
]

current_python_version = f'{version_info.major}.{version_info.minor}'

if current_python_version in invalid_python_versions:
    raise Exception(f'Invalid python version {current_python_version}')
else:
    print('Your python version seems fine. Proceeding ...')
