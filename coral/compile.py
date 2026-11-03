"""Compile contracts/RateRegistry.sol with solc 0.8.24 and cache the output."""
import json
from pathlib import Path

import solcx

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "contracts" / "RateRegistry.sol"
OUT = ROOT / "build" / "RateRegistry.json"
SOLC = "0.8.24"


def compile_registry(force: bool = False) -> dict:
    """Return {"abi": [...], "bin": "..."}. Recompiles when the source is newer."""
    if OUT.exists() and not force and OUT.stat().st_mtime >= SRC.stat().st_mtime:
        return json.loads(OUT.read_text())
    if SOLC not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(SOLC)
    solcx.set_solc_version(SOLC)
    compiled = solcx.compile_files(
        [str(SRC)], output_values=["abi", "bin"], optimize=True, optimize_runs=200, via_ir=True
    )
    key = next(k for k in compiled if k.endswith(":RateRegistry"))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(compiled[key]))
    return compiled[key]


if __name__ == "__main__":
    out = compile_registry(force=True)
    print(f"abi entries: {len(out['abi'])}, bytecode bytes: {len(out['bin']) // 2}")
