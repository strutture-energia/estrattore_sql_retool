"""
Decodifica del formato Transit usato da Retool in `page.data.appState`.

Copiato da `analizzatore_retool/src/minifier.py` (solo la parte di decoding,
senza blacklist): l'appState di un export Retool è una stringa Transit-JSON
(serializzazione di strutture Immutable.js) che va convertita in dict Python
prima di poter leggere i plugin.

NB: la libreria `transit-python` è vecchia (era Python 2). Per farla girare
su Python >= 3.10 applichiamo un piccolo monkey-patch al modulo
`collections`, che in origine esponeva le ABC spostate poi in
`collections.abc`. È una patch inerte se le ABC esistono già.
"""

from __future__ import annotations

# --- Monkey-patch obbligatorio PRIMA di importare `transit` ---
import collections
import collections.abc as _abc

for _name in (
    "Mapping", "MutableMapping", "Hashable",
    "Iterable", "Iterator", "Sequence", "MutableSequence",
    "Set", "MutableSet", "Container", "Sized", "Callable",
):
    if not hasattr(collections, _name):
        setattr(collections, _name, getattr(_abc, _name))
# --- Fine monkey-patch ---

from io import BytesIO
from typing import Any

from transit.reader import Reader  # type: ignore[import-untyped]
from transit.transit_types import (  # type: ignore[import-untyped]
    Boolean, Keyword, Symbol, TaggedValue, URI, frozendict,
)


def _normalize_transit(obj: Any) -> Any:
    """
    Converte ricorsivamente il risultato del decoder Transit in tipi
    Python standard (dict/list/str/...).

    Gestisce i tag immutable-* usati da Retool:
      - iR  (immutable Record): {n: nome, v: valori} -> dict piatto + chiave __iR__
      - iM  (immutable Map): serie [k,v,k,v,...] -> dict
      - iOM (immutable Ordered Map): come iM, mantenendo l'ordine
      - iL  (immutable List): array
    Tag sconosciuti vengono preservati come {"__tag__": ..., "rep": ...}.
    """
    if isinstance(obj, TaggedValue):
        tag = obj.tag
        rep = obj.rep

        if tag == "iR":
            rep_n = _normalize_transit(rep)
            inner = rep_n.get("v") if isinstance(rep_n, dict) else None
            if isinstance(inner, dict):
                return {"__iR__": rep_n.get("n"), **inner}
            return {"__iR__": rep_n.get("n"), "__value__": inner}

        if tag in ("iM", "iOM"):
            if isinstance(rep, (list, tuple)):
                out: dict[str, Any] = {}
                for i in range(0, len(rep) - 1, 2):
                    key = _normalize_transit(rep[i])
                    out[str(key)] = _normalize_transit(rep[i + 1])
                return out
            return _normalize_transit(rep)

        if tag == "iL":
            if isinstance(rep, (list, tuple)):
                return [_normalize_transit(x) for x in rep]
            return _normalize_transit(rep)

        return {"__tag__": tag, "rep": _normalize_transit(rep)}

    if isinstance(obj, (dict, frozendict)):
        return {str(k): _normalize_transit(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_normalize_transit(x) for x in obj]

    if isinstance(obj, (set, frozenset)):
        return [_normalize_transit(x) for x in obj]

    if isinstance(obj, Boolean):
        # Transit usa un wrapper custom; l'attributo `.v` contiene il bool Python.
        return bool(obj.v)

    if isinstance(obj, (Keyword, Symbol, URI)):
        return str(obj)

    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def decode_transit_string(transit_json: str) -> Any:
    """Decodifica una stringa Transit-JSON in un oggetto Python normalizzato."""
    reader = Reader("json")
    raw = reader.read(BytesIO(transit_json.encode("utf-8")))
    return _normalize_transit(raw)
