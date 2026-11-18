"""A stand-in for "the app the user is dictating into": one text field, in its own process.

Writes the field's contents to the file named on the command line whenever they change,
so a test can see exactly what a paste delivered.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLineEdit

out = Path(sys.argv[1])
app = QApplication(sys.argv)
field = QLineEdit()
field.setWindowTitle("yada e2e target")
field.resize(600, 60)
field.textChanged.connect(lambda text: out.write_text(text, encoding="utf-8"))
field.show()
field.activateWindow()
field.setFocus()
sys.exit(app.exec())
