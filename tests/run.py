"""Run the server test scenarios (tests/scenarios/*.py, functions named test_*).

usage: python tests/run.py [-k filter] [-v] [--routes]
  -k TEXT    only scenarios whose name contains TEXT
  -v         print every request and every remark
  --routes   also list the client routes no scenario called
Nothing touches the real database or logs: everything runs on a throwaway copy (see harness.py).
"""
import argparse
import glob
import importlib.util
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--routes", action="store_true")
    args = ap.parse_args()

    bench = harness.Bench(verbose=args.v)
    scenarios = []
    for path in sorted(glob.glob(os.path.join(harness.ROOT, "tests", "scenarios", "*.py"))):
        spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in dir(mod):
            if name.startswith("test_") and args.k in f"{mod.__name__}.{name}":
                scenarios.append((f"{mod.__name__}.{name}", getattr(mod, name)))

    results = []
    t0 = time.time()
    for name, fn in scenarios:
        bench.scenario = name
        print(f"• {name}")
        try:
            fn(bench)
            crash = None
        except harness.Failed as e:
            crash = f"échec : {e}"
        except Exception:
            crash = "exception :\n" + traceback.format_exc()
        errors = sum(1 for s, _, probs in bench.calls if s == name for lvl, _ in probs if lvl == "error")
        warns = sum(1 for s, _, probs in bench.calls if s == name for lvl, _ in probs if lvl == "warn")
        if crash:
            print("    " + crash.replace("\n", "\n    "))
        ok = not crash and not errors
        results.append((name, ok, errors, warns))
        print(f"    {'OK' if ok else 'ÉCHEC'}  ({errors} erreur(s), {warns} avertissement(s))")

    print()
    passed = sum(1 for r in results if r[1])
    print(f"{passed}/{len(results)} scénarios OK, {len(bench.calls)} requêtes, {time.time() - t0:.1f} s")
    if args.routes:
        with open(os.path.join(harness.ROOT, "docs", "api_routes.json"), encoding="utf-8") as f:
            known = {r for r in json.load(f) if "/" in r and not r.startswith(("user_data", "encryption"))}
        handled = {r.strip("/") for r in harness.api.ENDPOINTS}
        print("routes gérées par le serveur mais jamais appelées par un scénario :")
        print("  " + ", ".join(sorted(handled - bench.routes)) or "  (aucune)")
        print("routes du client sans code serveur :")
        print("  " + ", ".join(sorted(known - handled)))
    harness.cleanup()
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
