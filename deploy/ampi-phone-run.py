"""Starts the phone gateway or root's `ampi-phone` command from the root-owned copy of the code (python3 -I)."""
import sys

sys.path.insert(0, "/usr/local/lib/ampi-phone")
if len(sys.argv) > 1 and sys.argv[1] == "--admin":
    from ampi_phone.admin import main
    sys.exit(main(sys.argv[2:]))
from ampi_phone.gateway import main  # noqa: E402

main()