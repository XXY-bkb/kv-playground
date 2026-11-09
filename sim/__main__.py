"""让 `python -m sim` 等价于 `python -m sim.export`。"""

from .export import main

if __name__ == "__main__":
    main()
