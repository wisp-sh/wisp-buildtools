"""Budgets for raw application files and the encoded guest source program."""
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_PROGRAM_BYTES = 32 * 1024 * 1024
MAX_MODULES = 4096
MAX_FILES = 16384
GUEST_ENVIRONMENT = {
    'implementation_name': 'cpython', 'implementation_version': '3.14.0',
    'os_name': 'posix', 'platform_machine': 'wasm32', 'platform_release': '',
    'platform_system': 'WASI', 'platform_version': '',
    'platform_python_implementation': 'CPython', 'python_full_version': '3.14.0',
    'python_version': '3.14', 'sys_platform': 'wasi',
}
