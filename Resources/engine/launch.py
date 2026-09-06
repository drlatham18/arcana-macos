# -I ignores shell/PYTHONPATH customization; load only the app's bundled engine.
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from service import main
main()
