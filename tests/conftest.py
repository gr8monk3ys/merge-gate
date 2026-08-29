import os
import sys

# The scripts live at the repo root and import each other by bare name.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
