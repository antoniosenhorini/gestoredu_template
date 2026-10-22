#!/usr/bin/env python
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "colegio.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError:
        raise ImportError("Django não está instalado ou não encontrado")
    execute_from_command_line(sys.argv)
