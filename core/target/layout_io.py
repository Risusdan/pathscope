"""Writes GUI-edited block/edge/legend geometry back into a
hand-written <target>.topology.yaml without disturbing anything the
GUI didn't touch. A round-trip through yaml.safe_load + yaml.dump
would re-serialize the whole document and lose comments, key order
and the file's hand-tuned spacing - so this module never parses the
file into an object and re-emits it. It treats the text as lines and
only rewrites two kinds of region: the `layout:` section (regenerated
wholesale, since it is pure derived geometry with no hand content
worth preserving) and the `points:` payload of specific `edges:`
entries (rewritten in place, byte for byte identical apart from the
bracketed point list). Every other byte - blocks:, comments, odd
spacing, key order - is copied straight through."""
import os
import re
import tempfile
from typing import Callable, Dict, List, Optional, Tuple


class LayoutPatchError(Exception):
    pass


_LEGEND_LINE_RE = re.compile(r'^\s*legend:\s*\{')
_ENTRY_OPEN_RE = re.compile(r'^\s*-\s*\{')
_FROM_RE = re.compile(r'\bfrom:\s*([^\s,}]+)')
_TO_RE = re.compile(r'\bto:\s*([^\s,}]+)')
_LABEL_RE = re.compile(r'\blabel:\s*"?([^",}]*)"?')


def _detect_newline(text: str) -> str:
    # Regenerated lines must match the file's own line-ending style -
    # otherwise a CRLF-authored file would come back with bare \n on
    # every line this module writes, silently mixing endings. Decide
    # by majority so a handful of stray endings don't flip the style.
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    return "\r\n" if crlf > lf_only else "\n"


def _is_top_level_line(line: str) -> bool:
    stripped = line.rstrip("\r\n")
    return bool(stripped) and not stripped[0].isspace()


def _find_layout_line(lines: List[str]) -> Optional[int]:
    for idx, line in enumerate(lines):
        if line.strip() == "layout:":
            return idx
    return None


def _layout_section_end(lines: List[str], start: int) -> int:
    for idx in range(start + 1, len(lines)):
        if _is_top_level_line(lines[idx]):
            return idx
    return len(lines)


def _edge_identity(text: str) -> Tuple[str, str, str]:
    m_from = _FROM_RE.search(text)
    m_to = _TO_RE.search(text)
    m_label = _LABEL_RE.search(text)
    return (m_from.group(1) if m_from else "",
            m_to.group(1) if m_to else "",
            m_label.group(1) if m_label else "")


def _find_edge_entries(lines: List[str], start: int,
                       stop: int) -> List[Tuple[int, int]]:
    # An edge entry is a `- {...}` flow-mapping list item; the fixture
    # shows it either wholly on one physical line or wrapped after a
    # trailing comma, with the continuation indented. Edges never
    # nest a `{` (no dict-valued fields), so tracking brace balance
    # across physical lines finds the entry's closing line for either
    # shape without needing to know which shape it is up front.
    entries = []
    i = start
    while i < stop:
        line = lines[i]
        if not _ENTRY_OPEN_RE.match(line):
            i += 1
            continue
        depth = line.count("{") - line.count("}")
        j = i
        while depth > 0 and j + 1 < stop:
            j += 1
            depth += lines[j].count("{") - lines[j].count("}")
        entries.append((i, j))
        i = j + 1
    return entries


def _format_points(points) -> str:
    return "[" + ", ".join("[%d, %d]" % (px, py) for px, py in points) + "]"


def _replace_points_payload(line: str, payload: str) -> str:
    start = line.index("[", line.index("points:"))
    depth = 0
    end = start
    for k in range(start, len(line)):
        c = line[k]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = k
                break
    return line[:start] + payload + line[end + 1:]


def _insert_points(line: str, payload: str) -> str:
    idx = line.index("}")
    return line[:idx] + ", points: " + payload + line[idx:]


def _patch_edges(lines: List[str],
                 edge_points: Dict[Tuple[str, str, str], object],
                 stop: int) -> List[str]:
    lines = list(lines)
    edges_start = None
    for idx in range(stop):
        if lines[idx].strip() == "edges:":
            edges_start = idx
            break
    if edges_start is None:
        raise LayoutPatchError("no edges: section found")
    entries = _find_edge_entries(lines, edges_start + 1, stop)
    matched = set()
    for (i, j) in entries:
        identity = _edge_identity("".join(lines[i:j + 1]))
        if identity not in edge_points:
            continue
        matched.add(identity)
        value = edge_points[identity]
        if value is None:
            continue
        payload = _format_points(value)
        points_line = next((k for k in range(i, j + 1)
                            if "points:" in lines[k]), None)
        if points_line is not None:
            lines[points_line] = _replace_points_payload(
                lines[points_line], payload)
        else:
            lines[j] = _insert_points(lines[j], payload)
    missing = set(edge_points) - matched
    if missing:
        raise LayoutPatchError("edge(s) not found in file: %r"
                               % (sorted(missing),))
    return lines


def _format_field(name: str, value: int, width: int) -> str:
    pad = (width - len(str(value))) + 1
    return "%s: %d," % (name, value) + " " * pad


def _format_layout_lines(blocks, nl: str) -> List[str]:
    if not blocks:
        return []
    id_width = max(len(bid) for bid in blocks) + 2
    x_width = max(len(str(v[0])) for v in blocks.values())
    y_width = max(len(str(v[1])) for v in blocks.values())
    w_width = max(len(str(v[2])) for v in blocks.values())
    out = []
    for bid, (x, y, w, h) in blocks.items():
        id_field = (bid + ":").ljust(id_width)
        body = (_format_field("x", x, x_width)
                + _format_field("y", y, y_width)
                + _format_field("w", w, w_width)
                + "h: %d" % h)
        out.append("  %s{%s}%s" % (id_field, body, nl))
    return out


def patch_layout_text(original: str,
                      blocks: "OrderedDict[str, Tuple[int, int, int, int]]",
                      edge_points: "Dict[Tuple[str, str, str], Optional[List[Tuple[int, int]]]]",
                      legend: "Optional[Tuple[int, int]]") -> str:
    nl = _detect_newline(original)
    lines = original.splitlines(keepends=True)
    layout_idx = _find_layout_line(lines)

    if edge_points:
        scan_stop = layout_idx if layout_idx is not None else len(lines)
        lines = _patch_edges(lines, edge_points, scan_stop)

    carried_legend = None
    end_idx = None
    if layout_idx is not None:
        end_idx = _layout_section_end(lines, layout_idx)
        for line in lines[layout_idx + 1:end_idx]:
            if _LEGEND_LINE_RE.match(line):
                carried_legend = line
                break

    section = ["layout:" + nl] + _format_layout_lines(blocks, nl)
    if legend is not None:
        lx, ly = legend
        section.append("  legend: {x: %d, y: %d}%s" % (lx, ly, nl))
    elif carried_legend is not None:
        section.append(carried_legend)

    if layout_idx is None:
        prefix = list(lines)
        if prefix and not prefix[-1].endswith(("\r\n", "\n")):
            prefix[-1] = prefix[-1] + nl
        if prefix and prefix[-1].strip() != "":
            prefix.append(nl)
        new_lines = prefix + section
    else:
        new_lines = lines[:layout_idx] + section + lines[end_idx:]

    return "".join(new_lines)


def save_layout(path: str, blocks, edge_points, legend,
                validate: "Callable[[str], None]") -> None:
    try:
        # newline="" disables universal-newline translation on both
        # ends: a CRLF-authored file must come back out CRLF, and the
        # default text mode would silently flatten it to LF.
        with open(path, "r", newline="") as f:
            original = f.read()
        patched = patch_layout_text(original, blocks, edge_points, legend)
    except Exception as e:
        raise LayoutPatchError(str(e))

    dir_name = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".layout.",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(patched)
        validate(tmp_path)
        os.replace(tmp_path, path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise LayoutPatchError(str(e))
