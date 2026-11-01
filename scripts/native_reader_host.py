"""Native messaging entry point. stdout is reserved for framed JSON."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from comic_pipeline.local_launcher import main

if __name__=="__main__":
    main(sys.argv[1:],sys.stdin.buffer,sys.stdout.buffer)
