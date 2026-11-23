"""Starts the sign-in service from the root-owned copy of the code (python3 -I)."""
import sys

sys.path.insert(0, "/usr/local/lib/ampi-signin")
from ampi_phone.signin import main  # noqa: E402

main()
