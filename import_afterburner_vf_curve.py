#!/usr/bin/env python3
from afterburner.import_vf_curve import (
    AfterburnerProfileSelectionError,
    AfterburnerVfCurveSafetyError,
    main,
)
import sys


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except (AfterburnerProfileSelectionError, AfterburnerVfCurveSafetyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
