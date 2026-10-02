"""Pullarr entrypoint; legacy startup/environment/storage contracts stay intact."""

from runpy import run_module

if __name__ == '__main__':
    run_module('Kapowarr', run_name='__main__')
