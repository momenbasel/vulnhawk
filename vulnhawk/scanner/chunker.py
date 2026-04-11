"""Smart code chunker that splits codebases into analyzable pieces."""

from __future__ import annotations

import re
from pathlib import Path

from pathspec import PathSpec

from vulnhawk.models import CodeChunk, Language

# Default ignore patterns (gitignore style)
DEFAULT_IGNORE = [
    "node_modules/",
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".venv/",
    "venv/",
    "dist/",
    "build/",
    ".next/",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.sum",
    "*.svg",
    "*.png",
    "*.jpg",
    "*.gif",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    ".DS_Store",
    "coverage/",
    ".nyc_output/",
    "*.pb.go",
    "vendor/",
]

SUPPORTED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".php", ".rb", ".erb"}

# Max lines per chunk before splitting
MAX_CHUNK_LINES = 200
# Max file size to process (500KB)
MAX_FILE_SIZE = 500_000


def load_ignore_spec(target: Path) -> PathSpec:
    """Load .vulnhawkignore or .gitignore patterns."""
    patterns = list(DEFAULT_IGNORE)

    for ignore_file in [".vulnhawkignore", ".gitignore"]:
        ignore_path = target / ignore_file
        if ignore_path.exists():
            patterns.extend(ignore_path.read_text().splitlines())

    return PathSpec.from_lines("gitignore", patterns)


def discover_files(target: Path, ignore_spec: PathSpec) -> list[Path]:
    """Walk the target directory and collect scannable files."""
    files = []
    target = target.resolve()

    if target.is_file():
        if target.suffix in SUPPORTED_EXTENSIONS:
            return [target]
        return []

    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in SUPPORTED_EXTENSIONS:
            continue
        if path.stat().st_size > MAX_FILE_SIZE:
            continue

        rel = path.relative_to(target)
        if ignore_spec.match_file(str(rel)):
            continue

        files.append(path)

    return files


def extract_imports(content: str, language: Language) -> list[str]:
    """Extract import statements from source code."""
    imports = []

    if language == Language.PYTHON:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                imports.append(stripped)

    elif language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
        # import ... from '...'
        js_import_re = r"""(?:import|require)\s*(?:\(?\s*['"]([^'"]+)['"]|.+from\s+['"]([^'"]+)['"])"""
        for match in re.finditer(js_import_re, content):
            imports.append(match.group(0).strip())

    elif language == Language.GO:
        for match in re.finditer(r'import\s+(?:\(\s*([\s\S]*?)\s*\)|"([^"]+)")', content):
            imports.append(match.group(0).strip())

    elif language == Language.PHP:
        for line in content.splitlines():
            stripped = line.strip()
            if re.match(r"^(?:use|require|require_once|include|include_once|namespace)\s+", stripped):
                imports.append(stripped.rstrip(";"))

    elif language == Language.RUBY:
        for line in content.splitlines():
            stripped = line.strip()
            if re.match(r"^(?:require|require_relative|include|extend|prepend|load)\s+", stripped):
                imports.append(stripped)

    return imports


def _split_python(content: str, file_path: Path) -> list[CodeChunk]:
    """Split Python code into logical chunks."""
    chunks = []
    lines = content.splitlines(keepends=True)
    imports = extract_imports(content, Language.PYTHON)

    # Find top-level function and class definitions
    boundaries = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0 and re.match(r"^(async\s+)?(?:def|class)\s+", stripped):
            name_match = re.match(r"^(async\s+)?(?:def|class)\s+(\w+)", stripped)
            name = name_match.group(2) if name_match else "unknown"
            kind = "class" if name_match and name_match.group(1) is None and stripped.startswith("class") else \
                  "class" if name_match and name_match.group(1) and stripped.lstrip().startswith("async class") else \
                  "class" if stripped.startswith("class") else "function"
            boundaries.append((i, name, kind))

    if not boundaries:
        # No top-level definitions - treat whole file as one chunk
        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.PYTHON,
            content=content,
            start_line=1,
            end_line=len(lines),
            chunk_type="module",
            name=file_path.stem,
            imports=imports,
        ))
        return chunks

    # Module-level code before first definition
    if boundaries[0][0] > 0:
        module_content = "".join(lines[: boundaries[0][0]])
        if module_content.strip():
            chunks.append(CodeChunk(
                file_path=file_path,
                language=Language.PYTHON,
                content=module_content,
                start_line=1,
                end_line=boundaries[0][0],
                chunk_type="module",
                name=file_path.stem,
                imports=imports,
            ))

    # Each definition
    for idx, (start, name, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        chunk_content = "".join(lines[start:end])

        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.PYTHON,
            content=chunk_content,
            start_line=start + 1,
            end_line=end,
            chunk_type=kind,
            name=name,
            imports=imports,
        ))

    return chunks


def _split_js_ts(content: str, file_path: Path, language: Language) -> list[CodeChunk]:
    """Split JavaScript/TypeScript code into logical chunks."""
    chunks = []
    lines = content.splitlines(keepends=True)
    imports = extract_imports(content, language)

    # Find exported functions, classes, route handlers
    boundaries = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # function declarations
        if re.match(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+", stripped):
            name_match = re.search(r"function\s+(\w+)", stripped)
            name = name_match.group(1) if name_match else "anonymous"
            boundaries.append((i, name, "function"))
        # class declarations
        elif re.match(r"^(?:export\s+)?(?:default\s+)?class\s+\w+", stripped):
            name_match = re.search(r"class\s+(\w+)", stripped)
            name = name_match.group(1) if name_match else "anonymous"
            boundaries.append((i, name, "class"))
        # const/let arrow functions
        elif re.match(r"^(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?(?:\(|=>)", stripped):
            name_match = re.search(r"(?:const|let|var)\s+(\w+)", stripped)
            name = name_match.group(1) if name_match else "anonymous"
            boundaries.append((i, name, "function"))
        # Express/Fastify route handlers
        elif re.match(r"^(?:app|router|server)\.\s*(?:get|post|put|patch|delete|use)\s*\(", stripped):
            route_match = re.search(r"""['"](/[^'"]*?)['"]""", stripped)
            name = route_match.group(1) if route_match else "route"
            boundaries.append((i, name, "route"))

    if not boundaries:
        chunks.append(CodeChunk(
            file_path=file_path,
            language=language,
            content=content,
            start_line=1,
            end_line=len(lines),
            chunk_type="module",
            name=file_path.stem,
            imports=imports,
        ))
        return chunks

    # Process boundaries similar to Python
    if boundaries[0][0] > 0:
        module_content = "".join(lines[: boundaries[0][0]])
        if module_content.strip():
            chunks.append(CodeChunk(
                file_path=file_path,
                language=language,
                content=module_content,
                start_line=1,
                end_line=boundaries[0][0],
                chunk_type="module",
                name=file_path.stem,
                imports=imports,
            ))

    for idx, (start, name, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        chunk_content = "".join(lines[start:end])
        chunks.append(CodeChunk(
            file_path=file_path,
            language=language,
            content=chunk_content,
            start_line=start + 1,
            end_line=end,
            chunk_type=kind,
            name=name,
            imports=imports,
        ))

    return chunks


def _split_go(content: str, file_path: Path) -> list[CodeChunk]:
    """Split Go code into logical chunks."""
    chunks = []
    lines = content.splitlines(keepends=True)
    imports = extract_imports(content, Language.GO)

    boundaries = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^func\s+", stripped):
            name_match = re.search(r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)", stripped)
            name = name_match.group(1) if name_match else "unknown"
            kind = "function"
            # Check if it's an HTTP handler
            if re.search(r"http\.(?:Handler|HandlerFunc|ResponseWriter)", stripped):
                kind = "route"
            boundaries.append((i, name, kind))

    if not boundaries:
        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.GO,
            content=content,
            start_line=1,
            end_line=len(lines),
            chunk_type="module",
            name=file_path.stem,
            imports=imports,
        ))
        return chunks

    if boundaries[0][0] > 0:
        module_content = "".join(lines[: boundaries[0][0]])
        if module_content.strip():
            chunks.append(CodeChunk(
                file_path=file_path,
                language=Language.GO,
                content=module_content,
                start_line=1,
                end_line=boundaries[0][0],
                chunk_type="module",
                name=file_path.stem,
                imports=imports,
            ))

    for idx, (start, name, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        chunk_content = "".join(lines[start:end])
        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.GO,
            content=chunk_content,
            start_line=start + 1,
            end_line=end,
            chunk_type=kind,
            name=name,
            imports=imports,
        ))

    return chunks


def _split_php(content: str, file_path: Path) -> list[CodeChunk]:
    """Split PHP code into logical chunks."""
    chunks = []
    lines = content.splitlines(keepends=True)
    imports = extract_imports(content, Language.PHP)

    boundaries = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Class declarations
        if re.match(r"^(?:abstract\s+|final\s+)?class\s+\w+", stripped):
            name_match = re.search(r"class\s+(\w+)", stripped)
            name = name_match.group(1) if name_match else "unknown"
            boundaries.append((i, name, "class"))
        # Function/method declarations
        elif re.match(r"^(?:public|protected|private|static|\s)*function\s+\w+", stripped):
            name_match = re.search(r"function\s+(\w+)", stripped)
            name = name_match.group(1) if name_match else "unknown"
            boundaries.append((i, name, "function"))
        # Route definitions (Laravel)
        elif re.match(r"^Route::\s*(?:get|post|put|patch|delete|any|match)\s*\(", stripped):
            route_match = re.search(r"""['"](/[^'"]*?)['"]""", stripped)
            name = route_match.group(1) if route_match else "route"
            boundaries.append((i, name, "route"))
        # Trait declarations
        elif re.match(r"^trait\s+\w+", stripped):
            name_match = re.search(r"trait\s+(\w+)", stripped)
            name = name_match.group(1) if name_match else "unknown"
            boundaries.append((i, name, "class"))
        # Interface declarations
        elif re.match(r"^interface\s+\w+", stripped):
            name_match = re.search(r"interface\s+(\w+)", stripped)
            name = name_match.group(1) if name_match else "unknown"
            boundaries.append((i, name, "class"))

    if not boundaries:
        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.PHP,
            content=content,
            start_line=1,
            end_line=len(lines),
            chunk_type="module",
            name=file_path.stem,
            imports=imports,
        ))
        return chunks

    if boundaries[0][0] > 0:
        module_content = "".join(lines[: boundaries[0][0]])
        if module_content.strip():
            chunks.append(CodeChunk(
                file_path=file_path,
                language=Language.PHP,
                content=module_content,
                start_line=1,
                end_line=boundaries[0][0],
                chunk_type="module",
                name=file_path.stem,
                imports=imports,
            ))

    for idx, (start, name, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        chunk_content = "".join(lines[start:end])
        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.PHP,
            content=chunk_content,
            start_line=start + 1,
            end_line=end,
            chunk_type=kind,
            name=name,
            imports=imports,
        ))

    return chunks


def _split_ruby(content: str, file_path: Path) -> list[CodeChunk]:
    """Split Ruby code into logical chunks."""
    chunks = []
    lines = content.splitlines(keepends=True)
    imports = extract_imports(content, Language.RUBY)

    boundaries = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        stripped = stripped.rstrip()
        # Only top-level definitions (indent == 0)
        if indent == 0:
            # Class declarations
            if re.match(r"^class\s+\w+", stripped):
                name_match = re.search(r"class\s+(\w+)", stripped)
                name = name_match.group(1) if name_match else "unknown"
                boundaries.append((i, name, "class"))
            # Module declarations
            elif re.match(r"^module\s+\w+", stripped):
                name_match = re.search(r"module\s+(\w+)", stripped)
                name = name_match.group(1) if name_match else "unknown"
                boundaries.append((i, name, "class"))
            # Method definitions
            elif re.match(r"^def\s+\w+", stripped):
                name_match = re.search(r"def\s+(\w+)", stripped)
                name = name_match.group(1) if name_match else "unknown"
                boundaries.append((i, name, "function"))
        # Rails route definitions (any indent level)
        if re.match(r"^\s*(?:get|post|put|patch|delete|resources?|root)\s+", line):
            route_match = re.search(r"""['"](/[^'"]*?)['"]""", stripped)
            if route_match:
                boundaries.append((i, route_match.group(1), "route"))

    if not boundaries:
        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.RUBY,
            content=content,
            start_line=1,
            end_line=len(lines),
            chunk_type="module",
            name=file_path.stem,
            imports=imports,
        ))
        return chunks

    if boundaries[0][0] > 0:
        module_content = "".join(lines[: boundaries[0][0]])
        if module_content.strip():
            chunks.append(CodeChunk(
                file_path=file_path,
                language=Language.RUBY,
                content=module_content,
                start_line=1,
                end_line=boundaries[0][0],
                chunk_type="module",
                name=file_path.stem,
                imports=imports,
            ))

    for idx, (start, name, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        chunk_content = "".join(lines[start:end])
        chunks.append(CodeChunk(
            file_path=file_path,
            language=Language.RUBY,
            content=chunk_content,
            start_line=start + 1,
            end_line=end,
            chunk_type=kind,
            name=name,
            imports=imports,
        ))

    return chunks


def chunk_file(file_path: Path, content: str | None = None) -> list[CodeChunk]:
    """Split a single file into logical chunks."""
    if content is None:
        content = file_path.read_text(errors="replace")

    language = Language.from_extension(file_path.suffix)

    if language == Language.PYTHON:
        return _split_python(content, file_path)
    elif language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
        return _split_js_ts(content, file_path, language)
    elif language == Language.GO:
        return _split_go(content, file_path)
    elif language == Language.PHP:
        return _split_php(content, file_path)
    elif language == Language.RUBY:
        return _split_ruby(content, file_path)
    else:
        # Fallback: whole file as one chunk
        return [CodeChunk(
            file_path=file_path,
            language=language,
            content=content,
            start_line=1,
            end_line=len(content.splitlines()),
            chunk_type="module",
            name=file_path.stem,
            imports=[],
        )]


def chunk_codebase(target: Path) -> tuple[list[CodeChunk], list[Path]]:
    """Chunk an entire codebase. Returns (chunks, scanned_files)."""
    ignore_spec = load_ignore_spec(target)
    files = discover_files(target, ignore_spec)
    all_chunks = []

    for file_path in files:
        try:
            chunks = chunk_file(file_path)
            # Split oversized chunks
            for chunk in chunks:
                if chunk.content.count("\n") > MAX_CHUNK_LINES:
                    # For very large chunks, split by line count
                    lines = chunk.content.splitlines(keepends=True)
                    for i in range(0, len(lines), MAX_CHUNK_LINES):
                        sub_lines = lines[i : i + MAX_CHUNK_LINES]
                        all_chunks.append(CodeChunk(
                            file_path=chunk.file_path,
                            language=chunk.language,
                            content="".join(sub_lines),
                            start_line=chunk.start_line + i,
                            end_line=chunk.start_line + i + len(sub_lines),
                            chunk_type=chunk.chunk_type,
                            name=f"{chunk.name}_part{i // MAX_CHUNK_LINES}",
                            imports=chunk.imports,
                        ))
                else:
                    all_chunks.append(chunk)
        except (OSError, UnicodeDecodeError):
            continue

    return all_chunks, files
