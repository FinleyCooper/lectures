"""
Extract theorems, propositions, definitions, lemmas, and corollaries
from LaTeX lecture notes and export them as Anki-importable flashcards.

Output: anki_flashcards.txt (tab-separated, importable into Anki)
Format: Front\tBack\tTags
"""

import re
import os
import html
from pathlib import Path

WORKSPACE = Path(__file__).parent
ENV_TYPES = ["theorem", "proposition", "definition", "lemma", "corollary"]


def parse_macros(tex_source):
    """Parse \newcommand, \renewcommand, \DeclareMathOperator, and
    \DeclareDocumentCommand definitions from LaTeX source.
    Returns a list of (name, num_args, replacement) tuples.
    """
    macros = []

    # \newcommand{\name}[n]{replacement} or \newcommand{\name}{replacement}
    # Also handles \renewcommand
    for m in re.finditer(
        r'\\(?:re)?newcommand\{(\\[a-zA-Z]+)\}'
        r'(?:\[(\d)\])?'
        r'\{(.+?)\}\s*$',
        tex_source, re.MULTILINE
    ):
        name = m.group(1)  # e.g. \R
        num_args = int(m.group(2)) if m.group(2) else 0
        replacement = m.group(3)
        macros.append((name, num_args, replacement))

    # \DeclareMathOperator{\name}{text}
    for m in re.finditer(
        r'\\DeclareMathOperator\{(\\[a-zA-Z]+)\}\{([^}]+)\}',
        tex_source
    ):
        name = m.group(1)
        replacement = r'\operatorname{' + m.group(2) + '}'
        macros.append((name, 0, replacement))

    # \DeclareDocumentCommand\name{}{replacement}
    for m in re.finditer(
        r'\\DeclareDocumentCommand\\([a-zA-Z]+)\{\}\{([^}]+)\}',
        tex_source
    ):
        name = '\\' + m.group(1)
        replacement = m.group(2)
        macros.append((name, 0, replacement))

    # \DeclarePairedDelimiter\name{left}{right}  -> \left<left> #1 \right<right>
    for m in re.finditer(
        r'\\DeclarePairedDelimiter\\([a-zA-Z]+)\{([^}]+)\}\{([^}]+)\}',
        tex_source
    ):
        name = '\\' + m.group(1)
        left = m.group(2)
        right = m.group(3)
        replacement = '\\left' + left + ' #1 \\right' + right
        macros.append((name, 1, replacement))

    return macros


def expand_macros(text, macros):
    """Expand custom LaTeX macros in text.
    Applies expansions repeatedly to handle macros that reference other macros.
    """
    for _ in range(3):  # multiple passes for nested macro refs
        for name, num_args, replacement in macros:
            escaped_name = re.escape(name)
            if num_args == 0:
                # Simple replacement: \name followed by non-alpha (or end)
                pattern = escaped_name + r'(?![a-zA-Z])'

                text = re.sub(pattern, lambda m, r=replacement: r, text)
            elif num_args == 1:
                # \name{arg}
                pattern = escaped_name + r'\{([^}]*)\}'
                def make_replacer(repl):
                    def replacer(m):
                        return repl.replace('#1', m.group(1))
                    return replacer
                text = re.sub(pattern, make_replacer(replacement), text)
            elif num_args == 2:
                # \name{arg1}{arg2}
                pattern = escaped_name + r'\{([^}]*)\}\{([^}]*)\}'
                def make_replacer2(repl):
                    def replacer(m):
                        return repl.replace('#1', m.group(1)).replace('#2', m.group(2))
                    return replacer
                text = re.sub(pattern, make_replacer2(replacement), text)
    return text


def load_global_macros():
    """Load macros from header.sty."""
    header_path = WORKSPACE / 'header.sty'
    if header_path.exists():
        with open(header_path, 'r', encoding='utf-8') as f:
            return parse_macros(f.read())
    return []


def load_file_macros(filepath):
    """Load macros from a .tex file's preamble (before \begin{document})."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    # Only look at preamble
    doc_start = content.find(r'\begin{document}')
    if doc_start != -1:
        preamble = content[:doc_start]
    else:
        preamble = content
    return parse_macros(preamble)


# Regex to match \begin{env}...\end{env} (non-greedy, handles nesting by using a parser)
# We'll use a manual parser for robustness with nested environments.

def find_environments(tex_content, env_types):
    """Find all environments of the given types, handling nesting correctly."""
    results = []
    for env in env_types:
        pattern_begin = re.compile(r'\\begin\{' + env + r'\}')
        pos = 0
        while True:
            m = pattern_begin.search(tex_content, pos)
            if not m:
                break
            start = m.start()
            content_start = m.end()
            # Now find the matching \end{env}, handling nested \begin{env}
            depth = 1
            search_pos = content_start
            end_pos = None
            while depth > 0:
                next_begin = re.search(r'\\begin\{' + env + r'\}', tex_content[search_pos:])
                next_end = re.search(r'\\end\{' + env + r'\}', tex_content[search_pos:])
                if next_end is None:
                    break  # malformed
                if next_begin and next_begin.start() < next_end.start():
                    depth += 1
                    search_pos += next_begin.end()
                else:
                    depth -= 1
                    if depth == 0:
                        end_pos = search_pos + next_end.start()
                    search_pos += next_end.end()
            if end_pos is not None:
                body = tex_content[content_start:end_pos].strip()
                results.append((env, body, start))
            pos = content_start
    # Sort by position in file
    results.sort(key=lambda x: x[2])
    return results


def find_sections(tex_content):
    """Find all section/subsection headings with their positions."""
    pattern = re.compile(r'\\(section|subsection|subsubsection)\{([^}]+)\}')
    sections = []
    for m in pattern.finditer(tex_content):
        sections.append((m.start(), m.group(1), m.group(2)))
    return sections


def get_section_context(pos, sections):
    """Get the section/subsection context for a given position."""
    current = {}
    hierarchy = ["section", "subsection", "subsubsection"]
    for sec_pos, sec_type, sec_title in sections:
        if sec_pos > pos:
            break
        current[sec_type] = sec_title
        # Clear lower-level sections when a higher-level one is set
        idx = hierarchy.index(sec_type)
        for lower in hierarchy[idx + 1:]:
            current.pop(lower, None)
    parts = []
    for level in hierarchy:
        if level in current:
            parts.append(current[level])
    return " > ".join(parts) if parts else ""


def extract_title_from_body(body):
    """Extract a parenthesized title from the beginning of the body, e.g. '(Cauchy's theorem)'."""
    # Match a title in parentheses at the very start, possibly with whitespace/newline
    m = re.match(r'\s*\(([^)]+)\)', body)
    if m:
        title = m.group(1).strip()
        remaining = body[m.end():].strip()
        return title, remaining
    return None, body


def latex_to_anki_mathjax(text):
    """Convert LaTeX math delimiters to Anki-compatible MathJax/HTML format.
    
    Keeps the raw LaTeX but wraps it so Anki's MathJax can render it.
    Also converts newlines to <br> for Anki HTML display.
    """
    # Remove common LaTeX commands that are just formatting
    text = text.replace('\\qed', '')
    text = text.replace('\\par', '')
    
    # Convert display math environments to \[...\]
    # align* -> \[ \begin{aligned}...\end{aligned} \]
    text = re.sub(
        r'\\begin\{align\*?\}(.*?)\\end\{align\*?\}',
        lambda m: r'\[\begin{aligned}' + m.group(1) + r'\end{aligned}\]',
        text, flags=re.DOTALL
    )
    # equation* -> \[...\]
    text = re.sub(
        r'\\begin\{equation\*?\}(.*?)\\end\{equation\*?\}',
        lambda m: r'\[' + m.group(1) + r'\]',
        text, flags=re.DOTALL
    )
    
    # Convert enumerate to HTML lists
    def convert_enumerate(match):
        content = match.group(1)
        items = re.split(r'\\item\s*', content)
        items = [i.strip() for i in items if i.strip()]
        html_items = ''.join(f'<li>{item}</li>' for item in items)
        return f'<ol>{html_items}</ol>'
    
    text = re.sub(
        r'\\begin\{enumerate\}(?:\[[^\]]*\])?(.*?)\\end\{enumerate\}',
        convert_enumerate, text, flags=re.DOTALL
    )
    
    # Convert itemize to HTML lists
    def convert_itemize(match):
        content = match.group(1)
        items = re.split(r'\\item\s*', content)
        items = [i.strip() for i in items if i.strip()]
        html_items = ''.join(f'<li>{item}</li>' for item in items)
        return f'<ul>{html_items}</ul>'
    
    text = re.sub(
        r'\\begin\{itemize\}(.*?)\\end\{itemize\}',
        convert_itemize, text, flags=re.DOTALL
    )
    
    # Convert math delimiters to Anki MathJax-compatible format
    # First: display math $$...$$ -> \[...\]  (must come before inline $...$)
    text = re.sub(
        r'\$\$(.*?)\$\$',
        lambda m: r'\[' + m.group(1) + r'\]',
        text, flags=re.DOTALL
    )
    # Display math \[...\] is already correct for MathJax — leave as-is
    
    # Inline math $...$ -> \(...\)  (single $, not $$)
    # Match $ that is not preceded/followed by another $
    text = re.sub(
        r'(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)',
        lambda m: r'\(' + m.group(1) + r'\)',
        text, flags=re.DOTALL
    )
    
    # Remove \textit{...} -> <i>...</i>
    text = re.sub(r'\\textit\{([^}]*)\}', r'<i>\1</i>', text)
    text = re.sub(r'\\textbf\{([^}]*)\}', r'<b>\1</b>', text)
    text = re.sub(r'\\emph\{([^}]*)\}', r'<i>\1</i>', text)
    
    # Clean up multiple blank lines and convert to <br>
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        cleaned_lines.append(stripped)
    
    text = '<br>'.join(cleaned_lines)
    # Remove excessive <br> sequences
    text = re.sub(r'(<br>){3,}', '<br><br>', text)
    # Remove leading/trailing <br>
    text = text.strip('<br>')
    text = re.sub(r'^(<br>)+', '', text)
    text = re.sub(r'(<br>)+$', '', text)
    
    return text.strip()


def get_course_name(filepath):
    """Extract course name from the directory name."""
    return filepath.parent.name


def sanitize_tag(text):
    """Make a string safe for use as an Anki tag (no spaces, special chars)."""
    return text.replace(' ', '_').replace(',', '').replace('&', 'and')


def process_file(filepath, global_macros):
    """Process a single .tex file and return flashcard entries."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    course = get_course_name(filepath)
    file_macros = load_file_macros(filepath)
    all_macros = global_macros + file_macros
    
    sections = find_sections(content)
    environments = find_environments(content, ENV_TYPES)
    
    cards = []
    for env_type, body, pos in environments:
        title, clean_body = extract_title_from_body(body)
        # Expand custom macros before converting
        clean_body = expand_macros(clean_body, all_macros)
        if title:
            title = expand_macros(title, all_macros)
        section_ctx = get_section_context(pos, sections)
        
        # Build the front of the card
        type_label = env_type.capitalize()
        if title:
            front = f"<b>{type_label}</b>: {title}"
        else:
            # Use section context as hint
            front = f"<b>{type_label}</b>"
            if section_ctx:
                front += f" ({section_ctx})"
        
        front += f"<br><i>[{course}]</i>"
        
        # Build the back of the card
        back = latex_to_anki_mathjax(clean_body)
        
        # Tags
        tags = [sanitize_tag(course), sanitize_tag(env_type)]
        if section_ctx:
            # Add top-level section as tag
            top_section = section_ctx.split(' > ')[0]
            tags.append(sanitize_tag(top_section))
        
        tag_str = ' '.join(tags)
        
        cards.append((front, back, tag_str))
    
    return cards


def main():
    all_cards = []
    tex_files = sorted(WORKSPACE.rglob('main.tex'))
    global_macros = load_global_macros()
    print(f"Loaded {len(global_macros)} global macros from header.sty")
    
    print(f"Found {len(tex_files)} tex files to process:")
    for f in tex_files:
        course = get_course_name(f)
        print(f"  - {course}")
    
    for filepath in tex_files:
        course = get_course_name(filepath)
        cards = process_file(filepath, global_macros)
        print(f"  {course}: {len(cards)} cards extracted")
        all_cards.extend(cards)
    
    # Write TSV file for Anki import
    output_path = WORKSPACE / 'anki_flashcards.txt'
    with open(output_path, 'w', encoding='utf-8') as f:
        # Anki import header comments
        f.write('#separator:tab\n')
        f.write('#html:true\n')
        f.write('#tags column:3\n')
        f.write('#deck:Lecture Notes\n')
        for front, back, tags in all_cards:
            # Escape any tabs in content
            front_clean = front.replace('\t', ' ')
            back_clean = back.replace('\t', ' ')
            f.write(f'{front_clean}\t{back_clean}\t{tags}\n')
    
    print(f"\nTotal: {len(all_cards)} flashcards exported to {output_path}")
    print("Import into Anki: File > Import > select anki_flashcards.txt")
    print("Make sure MathJax is enabled in Anki (it is by default in Anki 2.1.54+)")


if __name__ == '__main__':
    main()
