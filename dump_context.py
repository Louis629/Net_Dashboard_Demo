import os
import sys
import fnmatch
import argparse
from pathlib import Path

# ==========================================
# CONFIGURATION & NOISE FILTERS
# ==========================================

# Include full Python source code in the output markdown?
INCLUDE_CODE_CONTENTS = True

# Max file size to dump (in KB) to avoid overflowing context limits
MAX_CODE_FILE_SIZE_KB = 50 

# Default output file name
DEFAULT_OUTPUT_FILENAME = "project_context.md"

# Directories to completely exclude from tree generation and code scanning
IGNORE_DIRS = {
    '.vs', '.git', '.venv', 'venv', 'env', '__pycache__', 
    '.pytest_cache', '.idea', '.vscode', 'build', 'dist', 
    'logs', 'node_modules', '*.egg-info'
}

# Wildcards and filenames to ignore in the directory tree
IGNORE_FILE_PATTERNS = {
    '*.pyc', '*.pyo', '*.pyd', '*.log', '*.zip', '*.sqlite', 
    '*.db', '*.vsidx', '*.backup.json', '*.suo', '*.user',
    'project_structure.md', 'project_structure.txt', 'project_context.md',
    'dump_context.py'
}

# Folders / Files to dump source code from (when INCLUDE_CODE_CONTENTS = True)
CODE_TARGET_PATHS = [
    Path("main.py"),
    Path("celery_app.py"),
    Path("providers"),
    Path("services"),
    Path("security"),
]

# Files to skip when dumping source code
CODE_EXCLUDE_FILES = {'_init_.py', '__init__.py'}


def is_ignored(path: Path) -> bool:
    """Checks if a path matches any ignored directory or file pattern."""
    for part in path.parts:
        if part in IGNORE_DIRS or any(fnmatch.fnmatch(part, pat) for pat in IGNORE_DIRS):
            return True
    
    if path.is_file():
        if path.name in IGNORE_FILE_PATTERNS or any(fnmatch.fnmatch(path.name, pat) for pat in IGNORE_FILE_PATTERNS):
            return True
            
    return False


def read_text_safe(path: Path) -> str:
    """Reads text gracefully handling UTF-8 BOM, UTF-16, and Latin-1 encodings."""
    for enc in ('utf-8-sig', 'utf-16', 'utf-8', 'latin-1'):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def build_tree(dir_path: Path, prefix: str = "") -> str:
    """Generates a clean directory tree ignoring build noise and IDE metadata."""
    tree_output = ""
    try:
        items = [p for p in dir_path.iterdir() if not is_ignored(p)]
    except PermissionError:
        return ""

    items.sort(key=lambda x: (x.is_file(), x.name.lower()))

    pointers = ['├── '] * (len(items) - 1) + ['└── ']
    for pointer, path in zip(pointers, items):
        tree_output += f"{prefix}{pointer}{path.name}\n"
        if path.is_dir():
            extension = '│   ' if pointer == '├── ' else '    '
            tree_output += build_tree(path, prefix=prefix + extension)
            
    return tree_output


def get_dependencies() -> str:
    """Reads requirements.txt cleanly, fixing BOM headers and extra line breaks."""
    req_file = Path("requirements.txt")
    if req_file.exists():
        raw_text = read_text_safe(req_file)
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        cleaned = "\n".join(lines)
        return f"# Cleaned from requirements.txt:\n{cleaned}"
    
    import subprocess
    try:
        output = subprocess.check_output(["pip", "freeze"], text=True).strip()
        return f"# Live pip freeze output:\n{output}"
    except Exception as err:
        return f"# Could not retrieve dependencies: {err}"


def get_source_code_dumps(root: Path) -> str:
    """Extracts source code from key modules into Markdown blocks."""
    output = []
    
    for target in CODE_TARGET_PATHS:
        full_target = root / target
        if not full_target.exists():
            continue
            
        files_to_process = []
        if full_target.is_file():
            files_to_process.append(full_target)
        else:
            files_to_process.extend([p for p in full_target.rglob("*.py") if not is_ignored(p)])
            
        files_to_process.sort(key=lambda x: str(x))

        for file_path in files_to_process:
            if file_path.name in CODE_EXCLUDE_FILES:
                continue
                
            if file_path.stat().st_size > MAX_CODE_FILE_SIZE_KB * 1024:
                continue

            rel_path = file_path.relative_to(root)
            code = read_text_safe(file_path).strip()
            
            if code:
                output.append(f"### File: `{rel_path}`\n```python\n{code}\n```\n")
                
    return "\n".join(output)


def generate_full_markdown(root: Path) -> str:
    """Assembles the complete Markdown payload."""
    lines = []
    lines.append("```markdown")
    lines.append(f"### Project Context: {root.name}\n")
    lines.append("#### Directory Tree")
    lines.append("```text")
    lines.append(f"{root.name}/")
    lines.append(build_tree(root).rstrip())
    lines.append("```\n")
    lines.append("#### Dependencies")
    lines.append("```text")
    lines.append(get_dependencies())
    lines.append("```\n")
    
    if INCLUDE_CODE_CONTENTS:
        lines.append("#### Key Source Code")
        lines.append(get_source_code_dumps(root))
        
    lines.append("```")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Export project context (tree, dependencies, source code) to Markdown.")
    parser.add_argument(
        "-o", "--output", 
        type=str, 
        default=DEFAULT_OUTPUT_FILENAME, 
        help=f"Output markdown filename (default: {DEFAULT_OUTPUT_FILENAME})"
    )
    parser.add_argument(
        "--stdout", 
        action="store_true", 
        help="Print directly to terminal instead of saving to a file"
    )
    
    args = parser.parse_args()
    root = Path.cwd()
    
    markdown_content = generate_full_markdown(root)
    
    if args.stdout:
        print(markdown_content)
    else:
        output_path = root / args.output
        output_path.write_text(markdown_content, encoding="utf-8")
        file_size_kb = output_path.stat().st_size / 1024
        print(f"[+] Context exported to '{args.output}' ({file_size_kb:.1f} KB)")


if __name__ == "__main__":
    main()