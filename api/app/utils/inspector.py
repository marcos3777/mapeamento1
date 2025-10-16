from pathlib import Path
import zipfile
import tempfile

try:
    import fiona  # opcional
    _HAS_FIONA = True
except Exception:
    _HAS_FIONA = False

def _zip_find_gdbs(zip_names: list[str]) -> list[str]:
    gdbs = set()
    for n in zip_names:
        parts = Path(n).parts
        for i, p in enumerate(parts):
            if p.lower().endswith(".gdb"):
                gdbs.add("/".join(parts[: i + 1]))
                break
    return sorted(gdbs)

def _zip_find_shp_layers(zip_names: list[str]) -> list[str]:
    bases = set()
    for n in zip_names:
        if n.lower().endswith(".shp"):
            bases.add(Path(n).with_suffix("").name)
    return sorted(bases)

def _list_gdb_layers(gdb_path: Path) -> list[str]:
    if not _HAS_FIONA:
        return []
    try:
        return list(fiona.listlayers(str(gdb_path)))
    except Exception:
        return []

def _inspect_zip(zip_path: Path, expand: bool) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()

    shp_layers = _zip_find_shp_layers(names)
    gdbs_in_zip = _zip_find_gdbs(names)

    gdb_layers = {}
    if expand and _HAS_FIONA and gdbs_in_zip:
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(td)
            for gdb in gdbs_in_zip:
                gdb_dir = Path(td) / gdb
                if gdb_dir.exists():
                    gdb_layers[gdb] = _list_gdb_layers(gdb_dir)

    return {
        "file": str(zip_path.name),
        "kind": "zip",
        "shapefiles": shp_layers,
        "gdbs": gdbs_in_zip,
        "gdb_layers": gdb_layers if gdb_layers else None,
        "used_fiona": _HAS_FIONA and expand,
    }

def _inspect_directory(dir_path: Path) -> dict:
    gdbs = sorted([p.name for p in dir_path.glob("*.gdb") if p.is_dir()])
    shp_layers = sorted({p.stem for p in dir_path.glob("*.shp")})
    gdb_layers = {}
    if _HAS_FIONA:
        for g in gdbs:
            layers = _list_gdb_layers(dir_path / g)
            gdb_layers[g] = layers
    return {
        "dir": str(dir_path),
        "kind": "folder",
        "shapefiles": shp_layers,
        "gdbs": gdbs,
        "gdb_layers": gdb_layers if gdb_layers else None,
        "used_fiona": _HAS_FIONA,
    }

def _is_zip_file(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except Exception:
        return False

def _extract_gdb_from_zip(zip_path: Path, gdb_prefix: str, out_dir: Path) -> Path:
    """Extrai somente a pasta .gdb (e seu conteúdo) do ZIP para out_dir."""
    gdb_prefix = gdb_prefix.rstrip("/") + "/"
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if n.startswith(gdb_prefix)]
        for name in members:
            z.extract(name, out_dir)
    return out_dir / gdb_prefix  

def _list_gdb_layers(gdb_dir: Path) -> list[str]:
    if not _HAS_FIONA:
        return []
    try:
        return list(fiona.listlayers(str(gdb_dir)))
    except Exception:
        return []

def _inspect_any_file(p: Path, deep: bool = False) -> dict:
    info = {"name": p.name, "size": p.stat().st_size, "zip_like": _is_zip_file(p)}
    if info["zip_like"]:
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
            shp_layers = sorted({Path(n).stem for n in names if n.lower().endswith(".shp")})
            gdbs = set()
            for n in names:
                parts = Path(n).parts
                for i, part in enumerate(parts):
                    if part.lower().endswith(".gdb"):
                        gdbs.add("/".join(parts[: i + 1]))
                        break
            gdbs = sorted(gdbs)
            info["zip"] = {"shapefiles": shp_layers, "gdbs": gdbs}
            if deep and _HAS_FIONA and gdbs:
                gdb_layers = {}
                with tempfile.TemporaryDirectory() as td:
                    td_path = Path(td)
                    for gdb in gdbs:
                        gdb_dir = _extract_gdb_from_zip(p, gdb, td_path)
                        if gdb_dir.exists():
                            gdb_layers[gdb] = _list_gdb_layers(gdb_dir)
                info["zip"]["gdb_layers"] = gdb_layers
        except Exception as e:
            info["zip_error"] = str(e)
    return info