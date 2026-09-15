"""Declaration extraction for wiring evidence — total, pure, and biased
toward the miss.

`review/symbols.py` answers "what does this file declare?" for two language
families. Every ambiguity here must resolve toward declaring nothing: a
missed symbol costs the reviewer a line of evidence, while an invented one
would have the block accuse a symbol that does not exist. These tests pin
both the widening (class bodies, JS/TS) and the ceilings that keep it from
becoming noise (closures, unexported bindings, dunders).
"""

from no_human.review.symbols import declared_symbols, language_of


def test_python_collects_module_level_and_class_bodies_with_qualified_names():
    source = (
        "def render(request):\n"
        "    return None\n"
        "\n"
        "class Store:\n"
        "    def close(self):\n"
        "        pass\n"
        "    class Cursor:\n"
        "        def fetch(self):\n"
        "            pass\n"
    )
    assert declared_symbols(source, "app.py") == {
        "render": "render",
        "Store": "Store",
        "Store.close": "close",
        "Store.Cursor": "Cursor",
        "Store.Cursor.fetch": "fetch",
    }


def test_python_skips_dunders_because_the_language_calls_them():
    source = (
        "class Store:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def __enter__(self):\n"
        "        pass\n"
        "    def open(self):\n"
        "        pass\n"
    )
    assert declared_symbols(source, "app.py") == {
        "Store": "Store", "Store.open": "open",
    }


def test_python_skips_definitions_inside_a_function_body():
    """A closure is file-private by construction, so "no reference outside
    this file" is true of nearly all of them and means nothing."""
    source = (
        "def outer():\n"
        "    def helper():\n"
        "        pass\n"
        "    class Local:\n"
        "        pass\n"
        "    return helper, Local\n"
    )
    assert declared_symbols(source, "app.py") == {"outer": "outer"}


def test_python_collects_async_definitions():
    source = "async def fetch_page(url):\n    return url\n"
    assert declared_symbols(source, "app.py") == {"fetch_page": "fetch_page"}


def test_python_that_does_not_parse_declares_nothing():
    assert declared_symbols("def (\n", "app.py") == {}


def test_python_with_a_null_byte_declares_nothing_rather_than_raising():
    assert declared_symbols("def a():\n    pass\n\x00", "app.py") == {}


def test_javascript_collects_exported_declarations():
    source = (
        "export function render(props) {}\n"
        "export default class Board {}\n"
        "export const Panel = () => null;\n"
        "export async function load() {}\n"
        "export let counter = 0;\n"
    )
    assert declared_symbols(source, "web/src/Board.jsx") == {
        "render": "render",
        "Board": "Board",
        "Panel": "Panel",
        "load": "load",
        "counter": "counter",
    }


def test_typescript_collects_interface_type_and_enum():
    source = (
        "export interface Shape { x: number }\n"
        "export type Alias = string;\n"
        "export enum Colour { Red }\n"
    )
    assert declared_symbols(source, "web/src/shape.ts") == {
        "Shape": "Shape", "Alias": "Alias", "Colour": "Colour",
    }


def test_javascript_skips_an_unexported_binding():
    """`const MAX = 10` beside its only use is the ordinary shape of a
    module, not an unwired symbol."""
    source = (
        "const MAX = 10;\n"
        "function helper() {}\n"
        "export function render() { return helper(MAX); }\n"
    )
    assert declared_symbols(source, "web/src/app.js") == {"render": "render"}


def test_javascript_skips_an_indented_declaration():
    """An `export` indented inside an ambient `declare module` block names a
    symbol defined somewhere else entirely, and this scanner has no block
    structure to tell that from a real one."""
    source = (
        'declare module "ext" {\n'
        "  export function helper(): void;\n"
        "}\n"
        "export const real = 1;\n"
    )
    assert declared_symbols(source, "web/src/ext.ts") == {"real": "real"}


def test_javascript_does_not_mine_a_commented_out_declaration():
    """A block comment is the one comment that reaches column zero on a line
    of its own, which is where this scanner looks."""
    source = (
        "// export function commented() {}\n"
        "/*\n"
        "export class Blocked {}\n"
        "*/\n"
        "export const real = 1;\n"
    )
    assert declared_symbols(source, "web/src/app.js") == {"real": "real"}


def test_javascript_does_not_mine_a_template_literal():
    source = (
        "export const tpl = `\n"
        "export class InTemplate {}\n"
        "`;\n"
        "export const after = 2;\n"
    )
    assert declared_symbols(source, "web/src/app.js") == {
        "tpl": "tpl", "after": "after",
    }


def test_an_apostrophe_in_jsx_text_costs_one_line_not_the_rest_of_the_file():
    """A JS string literal closes at the newline, and so does this scanner's
    quote state — otherwise `don't` in markup would blank every declaration
    below it."""
    source = (
        "export const Notice = () => <p>don't panic</p>;\n"
        "export function render() {}\n"
    )
    assert declared_symbols(source, "web/src/Notice.jsx") == {
        "Notice": "Notice", "render": "render",
    }


def test_a_re_export_list_declares_nothing():
    source = "export { render as alias } from './app.js';\n"
    assert declared_symbols(source, "web/src/index.js") == {}


def test_language_of_claims_only_what_a_reader_parses():
    assert language_of("a.py") == "python"
    for rel in ("a.js", "a.jsx", "a.mjs", "a.cjs", "a.ts", "a.tsx"):
        assert language_of(rel) == "javascript", rel
    for rel in ("a.md", "a.json", "a.go", "a.rs", "a.txt"):
        assert language_of(rel) is None, rel


def test_a_typescript_stub_is_not_a_declaration_source():
    """Every name in a `.d.ts` is defined elsewhere, so all of them would
    read as added-and-unreferenced."""
    assert language_of("web/src/env.d.ts") is None
    assert declared_symbols("export declare function f(): void;\n",
                            "web/src/env.d.ts") == {}


def test_an_unclaimed_language_declares_nothing():
    assert declared_symbols("func Handle() {}\n", "main.go") == {}
