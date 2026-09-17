"""yada — Yet Another Dictating App.

Hotkey, record, transcribe, optionally transform, paste. Windows 11 and KDE Plasma.
"""

import os

# NumPy's Windows wheel uses OpenBLAS, whose default is one worker per logical CPU.  Each
# worker commits a large native scratch arena even though yada only uses NumPy for
# elementwise audio conversion and level calculations, not BLAS operations.  On a 24-thread
# machine those unused arenas added roughly 740 MB to the idle process.  This must be set
# before anything imports NumPy; package initialisation is the earliest common path for the
# source and frozen applications.  Set it unconditionally because an inherited machine-wide
# value is just as capable of bloating this process, and yada has no workload that benefits
# from extra BLAS workers.
os.environ["OPENBLAS_NUM_THREADS"] = "1"

__version__ = "0.1.30"
