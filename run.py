"""Start Fabric Explorer:  python run.py [workspace address or ID]   (python run.py --help for options)"""
import sys

from fabric_explorer.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
