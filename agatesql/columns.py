"""
Helpers for preserving the SQL provenance of query result columns.

The DB-API cursor description only carries a label for each result column.  It
does not say which table a label came from, and result column labels may be
duplicated.  This module parses the (simple) SELECT statement around a result
so that each output position can be described independently of the values in
that column.
"""

from collections import OrderedDict, namedtuple
from collections.abc import Mapping

import agate
from sqlalchemy import Table as _SQL_TABLE
from sqlalchemy import inspect as sqlalchemy_inspect

Token = namedtuple('Token', ['kind', 'value', 'start', 'end', 'depth'])

_IDENT_START = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$')
_IDENT_BODY = _IDENT_START | set('0123456789')
_KEYWORDS = {
    'ALL', 'AND', 'ANY', 'AS', 'ASC', 'BETWEEN', 'BY', 'CASE', 'CAST', 'COLLATE',
    'CROSS', 'CURRENT_DATE', 'CURRENT_TIME', 'CURRENT_TIMESTAMP', 'DESC', 'DISTINCT',
    'ELSE', 'END', 'ESCAPE', 'EXCEPT', 'EXISTS', 'FALSE', 'FETCH', 'FILTER', 'FOR',
    'FROM', 'FULL', 'GROUP', 'HAVING', 'ILIKE', 'IN', 'INNER', 'INTERSECT', 'INTO',
    'IS', 'JOIN', 'LATERAL', 'LEFT', 'LIKE', 'LIMIT', 'NATURAL', 'NOT', 'NULL',
    'NULLS', 'OFFSET', 'ON', 'OR', 'ORDER', 'OUTER', 'OVER', 'PARTITION', 'RIGHT',
    'SELECT', 'THEN', 'TRUE', 'UNION', 'USING', 'WHEN', 'WHERE', 'WINDOW', 'WITH',
}
_TYPE_NAMES = {
    'BIGINT', 'BINARY', 'BIT', 'BLOB', 'BOOL', 'BOOLEAN', 'CHAR', 'CHARACTER',
    'DATE', 'DATETIME', 'DECIMAL', 'DOUBLE', 'FLOAT', 'INT', 'INTEGER', 'INTERVAL',
    'JSON', 'JSONB', 'MONEY', 'NUMERIC', 'REAL', 'SMALLINT', 'TEXT', 'TIME',
    'TIMESTAMP', 'TIMESTAMPTZ', 'UUID', 'VARBINARY', 'VARCHAR',
}
_CLAUSE_END = {
    'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'UNION',
    'EXCEPT', 'INTERSECT', 'WINDOW', 'FETCH', 'FOR',
}
_JOIN_WORDS = {'LEFT', 'RIGHT', 'FULL', 'INNER', 'OUTER', 'CROSS', 'NATURAL', 'LATERAL'}


class ColumnOrigin(Mapping):
    """
    Provenance for one column of a query result.

    The object is also a mapping, so ``origin.table`` and
    ``origin['table']`` are equivalent.  ``table`` and ``column`` are
    ``None`` when the expression cannot be traced to one physical table (for
    example ``a.id + b.id`` or a literal).
    """

    __slots__ = (
        'index', 'label', 'name', 'table', 'schema', 'alias', 'column', 'expression'
    )

    _keys = (
        'index', 'position', 'ordinal', 'label', 'original_label', 'name',
        'table', 'source_table', 'physical_table', 'schema', 'alias', 'column',
        'source_column', 'expression', 'known', 'unknown',
    )

    def __init__(self, index, label, name=None, table=None, schema=None, alias=None,
                 column=None, expression=None):
        self.index = index
        self.label = label
        self.name = name if name is not None else label
        self.table = table
        self.schema = schema
        self.alias = alias
        self.column = column
        self.expression = expression

    @property
    def position(self):
        return self.index

    @property
    def column_position(self):
        return self.index

    @property
    def result_index(self):
        return self.index

    @property
    def column_index(self):
        return self.index

    @property
    def sql_label(self):
        return self.label

    @property
    def source(self):
        return self.table

    @property
    def ordinal(self):
        """One-based position, for display purposes."""
        return self.index + 1

    @property
    def original_label(self):
        return self.label

    @property
    def source_table(self):
        return self.table

    @property
    def physical_table(self):
        return self.table

    @property
    def source_column(self):
        return self.column

    @property
    def known(self):
        return self.table is not None

    @property
    def unknown(self):
        return self.table is None

    def to_dict(self):
        return self.as_dict()

    def as_dict(self):
        return {
            'index': self.index,
            'position': self.index,
            'ordinal': self.index + 1,
            'label': self.label,
            'original_label': self.label,
            'name': self.name,
            'table': self.table,
            'source_table': self.table,
            'physical_table': self.table,
            'schema': self.schema,
            'alias': self.alias,
            'column': self.column,
            'source_column': self.column,
            'expression': self.expression,
            'known': self.known,
            'unknown': self.unknown,
        }

    def __getitem__(self, key):
        if key not in self._keys:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def __eq__(self, other):
        if isinstance(other, ColumnOrigin):
            return all(getattr(self, key) == getattr(other, key) for key in
                       ('index', 'label', 'name', 'table', 'schema', 'alias', 'column', 'expression'))
        if isinstance(other, Mapping):
            return self.as_dict() == dict(other)
        return NotImplemented

    def __hash__(self):
        return hash((self.index, self.label, self.name, self.table, self.schema,
                     self.alias, self.column, self.expression))

    def __repr__(self):
        return 'ColumnOrigin(index=%r, label=%r, name=%r, table=%r, column=%r, expression=%r)' % (
            self.index, self.label, self.name, self.table, self.column, self.expression
        )


def _lex(sql):
    """Tokenize enough SQL to locate the SELECT list and table references."""
    tokens = []
    length = len(sql)
    index = 0
    depth = 0

    while index < length:
        char = sql[index]

        if char.isspace():
            index += 1
            continue

        if sql.startswith('--', index):
            newline = sql.find('\n', index + 2)
            index = length if newline < 0 else newline + 1
            continue

        if sql.startswith('/*', index):
            end = sql.find('*/', index + 2)
            index = length if end < 0 else end + 2
            continue

        start = index

        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            tokens.append(Token('string', sql[start:index], start, index, depth))
            continue

        if char in ('"', '`'):
            quote = char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            tokens.append(Token('ident', _unquote_alias(sql[start:index]), start, index, depth))
            continue

        if char == '[':
            index += 1
            while index < length:
                if sql[index] == ']':
                    if index + 1 < length and sql[index + 1] == ']':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            tokens.append(Token('ident', sql[start + 1:index - 1].replace(']]', ']'), start, index, depth))
            continue

        if char == '(':
            tokens.append(Token('lparen', char, start, start + 1, depth))
            depth += 1
            index += 1
            continue

        if char == ')':
            depth = max(0, depth - 1)
            tokens.append(Token('rparen', char, start, start + 1, depth))
            index += 1
            continue

        if char == ',':
            tokens.append(Token('comma', char, start, start + 1, depth))
            index += 1
            continue

        if char == '.':
            tokens.append(Token('dot', char, start, start + 1, depth))
            index += 1
            continue

        if char == '*':
            tokens.append(Token('star', char, start, start + 1, depth))
            index += 1
            continue

        if char in _IDENT_START or ord(char) > 127:
            index += 1
            while index < length and (sql[index] in _IDENT_BODY or ord(sql[index]) > 127):
                index += 1
            value = sql[start:index]
            tokens.append(Token('word', value, start, index, depth))
            continue

        if char.isdigit():
            index += 1
            while index < length and (sql[index].isdigit() or sql[index] in '.eE+-'):
                index += 1
            tokens.append(Token('number', sql[start:index], start, index, depth))
            continue

        tokens.append(Token('other', char, start, start + 1, depth))
        index += 1

    return tokens


def _is_identifier(token):
    return token.kind in ('word', 'ident')


def _token_upper(token):
    return token.value.upper() if token.kind == 'word' else None


def _matching_paren(tokens, index):
    depth = tokens[index].depth
    for position in range(index + 1, len(tokens)):
        token = tokens[position]
        if token.kind == 'rparen' and token.depth == depth:
            return position
    return len(tokens) - 1


def _select_bounds(tokens):
    select_index = None
    from_index = None

    for index, token in enumerate(tokens):
        if token.depth != 0 or token.kind != 'word':
            continue
        upper = token.value.upper()
        if upper == 'SELECT' and select_index is None:
            select_index = index
            continue
        if select_index is not None and upper == 'FROM':
            from_index = index
            break

    if select_index is None:
        return None, None

    end_index = len(tokens)
    if from_index is None:
        for index in range(select_index + 1, len(tokens)):
            token = tokens[index]
            if token.depth == 0 and token.kind == 'word' and token.value.upper() in _CLAUSE_END:
                end_index = index
                break
    else:
        select_end = from_index
        for index in range(from_index + 1, len(tokens)):
            token = tokens[index]
            if token.depth == 0 and token.kind == 'word' and token.value.upper() in (
                    'where', 'group', 'having', 'order', 'limit', 'offset', 'union',
                    'except', 'intersect', 'window', 'fetch', 'for'):
                end_index = index
                break

    if from_index is not None:
        return (select_index + 1, select_end), (from_index, end_index)

    return (select_index + 1, end_index), (None, None)


def _split_top_level(tokens, start, end):
    parts = []
    part_start = start
    for index in range(start, end):
        if tokens[index].kind == 'comma' and tokens[index].depth == 0:
            parts.append((part_start, index))
            part_start = index + 1
    parts.append((part_start, end))
    return [(a, b) for a, b in parts if a < b]


def _strip_outer_parentheses(tokens, start, end):
    while end - start >= 2 and tokens[start].kind == 'lparen' and tokens[end - 1].kind == 'rparen':
        if _matching_paren(tokens, start) != end - 1:
            break
        start += 1
        end -= 1
    return start, end


def _unquote_alias(value):
    if isinstance(value, str) and len(value) >= 2:
        if value[0] == value[-1] and value[0] in ('"', '`'):
            quote = value[0]
            return value[1:-1].replace(quote + quote, quote)
        if value[0] == '[' and value[-1] == ']':
            return value[1:-1].replace(']]', ']')
    return value


def _expression_text(sql, tokens, start, end):
    if start >= end:
        return ''
    return sql[tokens[start].start:tokens[end - 1].end].strip()


def _split_alias(tokens, start, end):
    """Return ``(expression_end, alias)`` for one SELECT-list item."""
    expression_end = end
    alias = None

    last = end - 1
    if last < start:
        return expression_end, alias

    if _is_identifier(tokens[last]) and tokens[last].value.upper() in _KEYWORDS | _TYPE_NAMES:
        # ``... END`` and similar trailing keywords belong to the expression,
        # not to an implicit alias.
        return expression_end, alias

    if end - start >= 2 and tokens[end - 2].kind == 'word' and tokens[end - 2].value.upper() == 'AS':
        if _is_identifier(tokens[end - 1]):
            alias = tokens[end - 1].value
            expression_end = end - 2
    elif end - start >= 2 and _is_identifier(tokens[end - 1]):
        previous = tokens[end - 2]
        if previous.kind not in ('dot', 'other') and not (
                previous.kind == 'word' and previous.value.upper() in _KEYWORDS):
            # ``sum(x) total`` and ``a.id total`` are implicit aliases.  A
            # qualified name such as ``a.id`` has a dot before its last part.
            if previous.kind in ('rparen', 'word', 'ident', 'number', 'string', 'star'):
                if not (previous.kind == 'word' and previous.value.upper() in _TYPE_NAMES):
                    alias = tokens[end - 1].value
                    expression_end = end - 1

    return expression_end, alias


def _parse_column_parts(tokens, start, end):
    start, end = _strip_outer_parentheses(tokens, start, end)
    parts = []
    expect_identifier = True
    for index in range(start, end):
        token = tokens[index]
        if expect_identifier:
            if not _is_identifier(token):
                return None
            parts.append(token.value)
            expect_identifier = False
        else:
            if token.kind == 'dot':
                expect_identifier = True
            else:
                return None
    if expect_identifier or len(parts) > 4:
        return None
    return parts


class _Source:
    __slots__ = ('schema', 'table', 'alias', 'columns')

    def __init__(self, schema, table, alias, columns=None):
        self.schema = schema
        self.table = table
        self.alias = alias
        self.columns = columns

    @property
    def physical(self):
        return self.table is not None

    def __repr__(self):
        return '_Source(schema=%r, table=%r, alias=%r, columns=%r)' % (
            self.schema, self.table, self.alias, self.columns
        )


def _matching_word(tokens, index, word):
    return (0 <= index < len(tokens) and tokens[index].kind == 'word'
            and tokens[index].value.upper() == word)


def _parse_from_sources(tokens, start, end, cte_names):
    sources = []
    index = start

    def skip_join_words(position):
        while position < end:
            token = tokens[position]
            if token.kind == 'comma':
                return position + 1, True
            if _token_upper(token) in _JOIN_WORDS:
                position += 1
                continue
            if _matching_word(tokens, position, 'JOIN'):
                return position + 1, True
            return position, False
        return position, False

    first = True
    while index < end:
        if not first:
            index, begins_source = skip_join_words(index)
            if not begins_source or index >= end:
                break
        first = False

        schema = None
        table = None
        alias = None

        if tokens[index].kind == 'lparen':
            closing = _matching_paren(tokens, index)
            index = closing + 1
            table = None
        else:
            parts = []
            while index < end and _is_identifier(tokens[index]):
                parts.append(tokens[index].value)
                index += 1
                if index < end and tokens[index].kind == 'dot':
                    index += 1
                    continue
                break
            if not parts:
                while index < end and tokens[index].kind not in ('comma',):
                    if _token_upper(tokens[index]) == 'JOIN':
                        break
                    index += 1
                continue
            table = parts[-1]
            # ``schema.table`` and ``database.schema.table`` both name a
            # physical table; the leading catalog component (if any) has no
            # consequence for agate's table-level provenance.
            schema = parts[-2] if len(parts) >= 2 else None

            if index < end and tokens[index].kind == 'lparen':
                # A table-valued function (``json_each(...)``) is not a
                # physical table.
                index = _matching_paren(tokens, index) + 1
                table = None
                schema = None

            if index < end and _matching_word(tokens, index, 'AS'):
                index += 1
                if index < end and _is_identifier(tokens[index]):
                    alias = tokens[index].value
                    index += 1
            elif index < end and _is_identifier(tokens[index]):
                candidate = tokens[index].value
                upper = candidate.upper()
                if upper not in _KEYWORDS and upper not in _JOIN_WORDS and upper != 'ON':
                    alias = candidate
                    index += 1

        if table is not None and table.lower() in cte_names:
            table = None

        sources.append(_Source(schema, table, alias))

        # Skip ON/USING expressions until the next JOIN or comma.
        depth = 0
        while index < end:
            token = tokens[index]
            if token.kind == 'lparen':
                depth += 1
            elif token.kind == 'rparen':
                depth = max(0, depth - 1)
            elif depth == 0:
                if token.kind == 'comma':
                    break
                if _matching_word(tokens, index, 'JOIN'):
                    break
            index += 1

    return sources


def _with_ctes(tokens):
    names = set()
    if not tokens or not _matching_word(tokens, 0, 'WITH'):
        return names

    index = 1
    if _matching_word(tokens, index, 'RECURSIVE'):
        index += 1

    while index < len(tokens) and tokens[index].depth == 0 and _is_identifier(tokens[index]):
        name = tokens[index].value.lower()
        index += 1
        if index < len(tokens) and tokens[index].kind == 'lparen':
            index = _matching_paren(tokens, index) + 1
        if not _matching_word(tokens, index, 'AS'):
            break
        index += 1
        if _matching_word(tokens, index, 'NOT'):
            index += 1
        if _matching_word(tokens, index, 'MATERIALIZED'):
            index += 1
        if index >= len(tokens) or tokens[index].kind != 'lparen':
            break
        index = _matching_paren(tokens, index) + 1
        names.add(name)
        if index < len(tokens) and tokens[index].kind == 'comma':
            index += 1
            continue
        break
    return names


def _load_source_columns(connection, source):
    if source.table is None or connection is None:
        return None
    try:
        inspector = sqlalchemy_inspect(connection)
        columns = inspector.get_columns(source.table, schema=source.schema)
    except Exception:
        return None
    return tuple(column['name'] for column in columns)


def _find_source(sources, qualifier):
    qualifier_lower = qualifier.lower() if isinstance(qualifier, str) else qualifier
    matches = []
    for source in sources:
        if source.alias and source.alias.lower() == qualifier_lower:
            matches.append(source)
        elif source.table and source.table.lower() == qualifier_lower:
            matches.append(source)
    return matches[0] if len(matches) == 1 else None


def _source_identity(source):
    return (source.schema, source.table, source.alias)


_LITERAL_WORDS = {
    'NULL', 'TRUE', 'FALSE', 'CURRENT_DATE', 'CURRENT_TIME', 'CURRENT_TIMESTAMP',
    'CURRENT_USER', 'SESSION_USER', 'LOCALTIME', 'LOCALTIMESTAMP',
}


def _resolve_parts(parts, sources, source_columns):
    column = parts[-1]

    if column.upper() in _LITERAL_WORDS:
        return None, None

    if len(parts) == 1:
        matches = []
        for source in sources:
            columns = source_columns.get(_source_identity(source))
            if columns is None:
                continue
            if any(name.lower() == column.lower() for name in columns):
                matches.append(source)
        if len(matches) == 1:
            return matches[0], column
        if len(matches) > 1:
            return None, column
        if len(sources) == 1 and not any(
                source_columns.get(_source_identity(source)) is not None for source in sources):
            # Without schema information, a sole source is the only possible
            # origin.  With schema information, an unknown bare name stays
            # unknown instead of being attributed to the one table present.
            return sources[0], column
        return None, column

    if len(parts) == 2:
        source = _find_source(sources, parts[0])
        return source, column

    schema = parts[-3]
    table_name = parts[-2]
    for source in sources:
        if source.table and source.table.lower() == table_name.lower():
            if source.schema is None or source.schema.lower() == schema.lower():
                return source, column
    return None, column


def _expression_source(tokens, start, end, sources, source_columns):
    """Return ``(source, source_column)`` for an expression, or ``(None, column)``."""
    parts = _parse_column_parts(tokens, start, end)
    if parts is not None:
        return _resolve_parts(parts, sources, source_columns)

    inner_start, inner_end = _strip_outer_parentheses(tokens, start, end)

    # A nested query has its own scope; a column name inside it must not be
    # attributed to the outer query's tables.  (A CASE expression has no
    # FROM clause, so its references still belong to the outer query.)
    for index in range(start, end):
        token = tokens[index]
        if token.kind == 'word' and token.value.upper() == 'SELECT':
            return None, None

    referenced = []
    unresolved = False
    saw_star = False
    skip_next = False

    index = inner_start
    while index < inner_end:
        token = tokens[index]
        if token.kind == 'word' and token.value.upper() == 'AS':
            skip_next = True
            index += 1
            continue
        if skip_next:
            skip_next = False
            index += 1
            continue

        if token.kind == 'star':
            saw_star = True
            index += 1
            continue

        if not _is_identifier(token):
            index += 1
            continue

        # Function names are not column references.
        if index + 1 < inner_end and tokens[index + 1].kind == 'lparen':
            index += 2
            continue

        if token.kind == 'word' and (
                token.value.upper() in _KEYWORDS or token.value.upper() in _TYPE_NAMES):
            index += 1
            # ``NULL::integer`` and ``CAST(x AS integer)`` shouldn't leave a
            # type name looking like a column reference.
            if token.value.upper() == 'NULL':
                while index + 1 < inner_end and tokens[index].kind == 'other' and tokens[index].value == ':':
                    index += 2
            continue

        parts = [token.value]
        position = index + 1
        while position + 1 < inner_end and tokens[position].kind == 'dot' and _is_identifier(tokens[position + 1]):
            parts.append(tokens[position + 1].value)
            position += 2

        if len(parts) == 1:
            source, _ = _resolve_parts(parts, sources, source_columns)
            if source is None:
                unresolved = True
            else:
                referenced.append(source)
        else:
            source, _ = _resolve_parts(parts, sources, source_columns)
            if source is None:
                unresolved = True
            else:
                referenced.append(source)

        index = position

    if saw_star:
        if len(sources) == 1:
            referenced.append(sources[0])
        elif sources:
            unresolved = True

    if unresolved:
        return None, None

    distinct = []
    for source in referenced:
        if _source_identity(source) not in [_source_identity(item) for item in distinct]:
            distinct.append(source)

    if len(distinct) == 1:
        return distinct[0], None
    return None, None


def _origin_for_expression(expression, tokens, start, end, sources, source_columns):
    source, column = _expression_source(tokens, start, end, sources, source_columns)
    if source is not None and source.physical:
        return {
            'table': source.table,
            'schema': source.schema,
            'alias': source.alias,
            'column': column,
            'expression': expression,
        }
    if source is not None:
        # A derived table (``FROM (SELECT ...) x``) has a physical alias but no
        # physical base table to report.
        return {
            'table': None,
            'schema': None,
            'alias': source.alias,
            'column': column,
            'expression': expression,
        }
    return {'table': None, 'schema': None, 'alias': None, 'column': None, 'expression': expression}


def _star_sources(tokens, start, end, sources, source_columns):
    start, end = _strip_outer_parentheses(tokens, start, end)
    if end - start == 1 and tokens[start].kind == 'star':
        return sources
    if end - start == 3 and _is_identifier(tokens[start]) and tokens[start + 1].kind == 'dot' \
            and tokens[start + 2].kind == 'star':
        source = _find_source(sources, tokens[start].value)
        return [source] if source is not None else []
    return None


def _draft(label, table=None, schema=None, alias=None, column=None, expression=None):
    return {
        'label': label,
        'table': table,
        'schema': schema,
        'alias': alias,
        'column': column,
        'expression': expression,
    }


def _skip_select_decorations(tokens, start, end):
    """Skip DISTINCT, ALL, TOP and SQL_CALC_FOUND_ROWS before the select list."""
    while start < end and tokens[start].kind == 'word':
        word = tokens[start].value.upper()
        if word == 'DISTINCT':
            start += 1
            if start < end and tokens[start].kind == 'word' and tokens[start].value.upper() == 'ON':
                start += 1
                if start < end and tokens[start].kind == 'lparen':
                    start = _matching_paren(tokens, start) + 1
            continue
        if word == 'ALL':
            start += 1
            continue
        if word == 'SQL_CALC_FOUND_ROWS':
            start += 1
            continue
        if word == 'TOP':
            start += 1
            if start < end and tokens[start].kind == 'lparen':
                start = _matching_paren(tokens, start) + 1
            elif start < end and tokens[start].kind == 'number':
                start += 1
            while start < end and tokens[start].kind == 'word' and tokens[start].value.upper() in (
                    'PERCENT', 'WITH', 'TIES'):
                start += 1
            continue
        break
    return start


def _drafts_from_sql(sql, tokens, select_start, select_end, sources, source_columns):
    if select_start is None:
        return None

    start = _skip_select_decorations(tokens, select_start, select_end)

    drafts = []
    unknown_star = None

    for item_start, item_end in _split_top_level(tokens, start, select_end):
        expression_end, alias = _split_alias(tokens, item_start, item_end)
        star_sources = _star_sources(tokens, item_start, expression_end, sources, source_columns)
        if star_sources is not None:
            if not star_sources:
                if unknown_star is None:
                    unknown_star = len(drafts)
                    drafts.append(None)
                continue
            expanded = False
            for source in star_sources:
                columns = source_columns.get(_source_identity(source))
                if columns is None:
                    expanded = False
                    break
                for column in columns:
                    drafts.append(_draft(
                        column,
                        table=source.table if source.physical else None,
                        schema=source.schema if source.physical else None,
                        alias=source.alias,
                        column=column,
                        expression='%s.*' % (source.alias or source.table or ''),
                    ))
                expanded = True
            if not expanded:
                if unknown_star is None:
                    unknown_star = len(drafts)
                    drafts.append(None)
            continue

        expression = _expression_text(sql, tokens, item_start, expression_end)
        if alias is not None:
            label = _unquote_alias(alias)
        else:
            parts = _parse_column_parts(tokens, item_start, expression_end)
            label = _unquote_alias(parts[-1]) if parts else expression
        origin = _origin_for_expression(expression, tokens, item_start, expression_end,
                                        sources, source_columns)
        drafts.append(_draft(label, **origin))

    return drafts, unknown_star


def _origins_from_statement(statement, labels):
    """Use SQLAlchemy's expression tree when the caller supplied a Select."""
    selected = getattr(statement, 'selected_columns', None)
    if selected is None:
        selected = getattr(statement, '_all_selected_columns', None)
    if selected is None:
        return None
    try:
        selected = list(selected)
    except TypeError:
        return None
    if len(selected) != len(labels):
        return None

    drafts = []
    for column, label in zip(selected, labels):
        element = getattr(column, 'element', column)
        table = getattr(element, 'table', None)
        alias = None
        physical = isinstance(table, _SQL_TABLE)
        if not physical:
            # ``Table.alias()`` returns an ``Alias`` whose element is the base
            # table.  A subquery has no ``element`` and stays unknown.
            alias = getattr(table, 'name', None)
            table = getattr(table, 'element', None)
            physical = isinstance(table, _SQL_TABLE)
        drafts.append(_draft(
            label,
            table=table.name if physical else None,
            schema=table.schema if physical else None,
            alias=alias,
            column=getattr(element, 'name', None) if physical else None,
            expression=str(column),
        ))
    return drafts


def _fallback_drafts(labels):
    return [_draft(label) for label in labels]


def _fill_unknown_star(drafts, unknown_star, labels):
    known_count = sum(1 for draft in drafts if draft is not None)
    missing = len(labels) - known_count
    if unknown_star is None or missing < 0:
        return None

    filled = []
    label_index = 0
    for draft in drafts:
        if draft is None:
            for _ in range(missing):
                filled.append(_draft(labels[label_index]))
                label_index += 1
        else:
            filled.append(draft)
            label_index += 1
    if label_index != len(labels):
        return None
    return filled


def _origins_from_statement_text(statement, labels):
    """Fallback to the compiled text of a statement the expression tree can't describe."""
    try:
        text = str(statement)
    except Exception:
        return None
    try:
        return _drafts_from_text(text, labels)
    except Exception:
        return None


def _drafts_from_text(sql, labels):
    tokens = _lex(sql)
    cte_names = _with_ctes(tokens)
    select_range, from_range = _select_bounds(tokens)
    sources = []
    if from_range[0] is not None:
        sources = _parse_from_sources(tokens, from_range[0] + 1, from_range[1], cte_names)

    if select_range[0] is None:
        return None

    parsed = _drafts_from_sql(sql, tokens, select_range[0], select_range[1],
                              sources, _source_column_info(sources))
    return parsed[0]


def _source_column_info(sources, connection=None):
    source_columns = OrderedDict()
    for source in sources:
        source_columns[_source_identity(source)] = _load_source_columns(connection, source)
    return source_columns


def build_origins(sql, labels, connection=None, statement=None):
    """
    Build one :class:`ColumnOrigin` per result position.

    ``sql`` may be either the original SQL string or ``None`` when a
    SQLAlchemy statement is available.  ``labels`` are the labels returned by
    the DB-API cursor.
    """
    labels = list(labels)
    unknown_star = None

    if statement is not None:
        drafts = _origins_from_statement(statement, labels)
        if drafts is not None and any(draft.get('table') is None for draft in drafts):
            # Functions such as ``count(*)`` have no column-level table on the
            # expression tree.  The compiled text can resolve them against the
            # FROM clause more precisely.
            text_drafts = _origins_from_statement_text(statement, labels)
            if text_drafts is not None and len(text_drafts) == len(drafts):
                drafts = text_drafts
    else:
        drafts = None

    if (drafts is None or len(drafts) != len(labels)) and sql is None and statement is not None:
        try:
            sql = str(statement)
        except Exception:
            sql = None

    if (drafts is None or len(drafts) != len(labels)) and isinstance(sql, str):
        tokens = _lex(sql)
        cte_names = _with_ctes(tokens)
        select_range, from_range = _select_bounds(tokens)
        sources = []
        if from_range[0] is not None:
            sources = _parse_from_sources(tokens, from_range[0] + 1, from_range[1], cte_names)

        source_columns = _source_column_info(sources, connection)

        parsed = None
        if select_range[0] is not None:
            parsed = _drafts_from_sql(sql, tokens, select_range[0], select_range[1],
                                      sources, source_columns)
        if parsed is not None and (drafts is None or len(parsed[0]) == len(labels)):
            drafts, unknown_star = parsed

    if drafts is None and statement is not None:
        drafts = _origins_from_statement_text(statement, labels)

    if drafts is None:
        drafts = _fallback_drafts(labels)
        unknown_star = None
    elif len(drafts) != len(labels):
        # A single unexpandable ``*`` can be accounted for by position.  This
        # still leaves those positions explicitly unknown.
        filled = _fill_unknown_star(drafts, unknown_star, labels)
        drafts = filled if filled is not None else _fallback_drafts(labels)

    # The cursor labels are authoritative; the parser only supplies origin.
    for draft, label in zip(drafts, labels):
        draft['label'] = label

    origins = _assign_names(drafts)

    if len(origins) == len(labels):
        _warn_about_renames(origins)

    return origins


def _warn_about_renames(origins):
    for origin in origins:
        if origin.name != origin.label:
            agate.utils.warn_duplicate_column(origin.label, origin.name)


def _assign_names(drafts):
    reserved = {draft['label'] for draft in drafts}
    used = set()
    origins = []

    for index, draft in enumerate(drafts):
        label = draft['label']
        if label not in used:
            name = label
        else:
            suffix = 2
            name = '%s_%d' % (label, suffix)
            # Never steal a label that belongs to another result column.
            while name in reserved or name in used:
                suffix += 1
                name = '%s_%d' % (label, suffix)

        while name in used:
            suffix = 2
            name = '%s_%d' % (label, suffix)
            while name in reserved or name in used:
                suffix += 1
                name = '%s_%d' % (label, suffix)

        used.add(name)
        origins.append(ColumnOrigin(
            index=index,
            label=label,
            name=name,
            table=draft.get('table'),
            schema=draft.get('schema'),
            alias=draft.get('alias'),
            column=draft.get('column'),
            expression=draft.get('expression'),
        ))

    return tuple(origins)


class PositionList(tuple):
    """A tuple of result positions that also compares equal to a list."""

    def __eq__(self, other):
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return tuple.__eq__(self, other)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = tuple.__hash__


class LabelPositions(OrderedDict):
    """Mapping of original SQL label to one or more result positions."""

    def as_dict(self):
        return OrderedDict((label, list(positions)) for label, positions in self.items())


class ColumnOrigins(tuple):
    """
    An immutable sequence of :class:`ColumnOrigin`, indexed by position.

    For convenience it also supports lookup by original label.  A label that
    occurs once returns that :class:`ColumnOrigin`; a duplicated label returns
    a tuple of every origin sharing it.  The sequence is callable so that it
    can be used either as ``table.column_origins`` or
    ``table.column_origins()``.
    """

    def __new__(cls, origins):
        return tuple.__new__(cls, tuple(origins))

    @property
    def labels(self):
        return tuple(origin.label for origin in self)

    @property
    def positions(self):
        positions = LabelPositions()
        for origin in self:
            positions.setdefault(origin.label, []).append(origin.index)
        return LabelPositions(
            (label, PositionList(indexes)) for label, indexes in positions.items()
        )

    @property
    def names(self):
        return tuple(origin.name for origin in self)

    @property
    def tables(self):
        return tuple(origin.table for origin in self)

    @property
    def source_columns(self):
        return tuple(origin.column for origin in self)

    @property
    def unknown(self):
        return tuple(origin for origin in self if origin.table is None)

    def by_label(self):
        grouped = OrderedDict()
        for origin in self:
            grouped.setdefault(origin.label, []).append(origin)
        return OrderedDict((label, tuple(origins)) for label, origins in grouped.items())

    def by_name(self):
        """Return ``name -> origin`` for the table's unambiguous column names."""
        return OrderedDict((origin.name, origin) for origin in self)

    def get(self, key, default=None):
        if isinstance(key, int):
            try:
                return self[key]
            except IndexError:
                return default
        matches = tuple(origin for origin in self if origin.label == key)
        if not matches:
            return default
        if len(matches) == 1:
            return matches[0]
        return matches

    def keys(self):
        return self.labels

    def values(self):
        return tuple(self)

    def items(self):
        return tuple((origin.label, origin) for origin in self)

    def dict(self):
        return self.by_label()

    def as_dicts(self):
        return tuple(origin.as_dict() for origin in self)

    def report(self):
        """Return one ``{label, position, table, column, name}`` dict per column."""
        return tuple(
            {
                'label': origin.label,
                'position': origin.index,
                'name': origin.name,
                'table': origin.table,
                'column': origin.column,
            }
            for origin in self
        )

    def __call__(self):
        return self

    def __getitem__(self, key):
        if isinstance(key, str):
            matches = tuple(origin for origin in self if origin.label == key)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return matches
            for origin in self:
                if origin.name == key:
                    return origin
            raise KeyError(key)
        return tuple.__getitem__(self, key)

    def __getattr__(self, name):
        if name in ColumnOrigin._keys:
            return tuple(getattr(origin, name) for origin in self)
        raise AttributeError(name)


def attach_origins(table, origins):
    """Attach provenance to an agate Table without altering its public schema."""
    origins = ColumnOrigins(origins)

    # ``Table`` may still normalize an unusual (for example empty) name.  The
    # origin must always describe the name the table actually exposes.
    for origin, name in zip(origins, table.column_names):
        origin.name = name

    table._sql_column_origins = origins
    table.original_column_names = origins.labels
    table.original_column_labels = origins.labels
    table.original_labels = origins.labels
    table.column_metadata = origins
    table.column_info = origins
    table.original_columns = origins
    table.sql_column_labels = origins.labels
    table.sql_column_positions = origins.positions
    table.column_position_map = origins.positions
    table.label_positions = origins.positions
    table.original_label_positions = origins.positions
    table.column_tables = origins.tables
    table.sql_columns_by_label = table.columns_by_label
    return table


def get_origins(table):
    """Return provenance for every column, synthesizing identity metadata if absent."""
    origins = getattr(table, '_sql_column_origins', None)
    if origins is not None and len(origins) == len(table.column_names):
        return origins

    return ColumnOrigins(
        ColumnOrigin(index=index, label=name, name=name, column=name)
        for index, name in enumerate(table.column_names)
    )


class ColumnGroup(tuple):
    """
    The agate columns that share one original SQL label.

    The group is always a tuple, so a duplicated label never silently returns
    one side.  When there is exactly one match, common ``Column`` attributes
    such as ``name`` and ``values()`` are proxied to that column for
    convenience.
    """

    def _single(self):
        if len(self) != 1:
            raise AttributeError('This SQL label matches %d columns; use an index.' % len(self))
        return self[0]

    @property
    def name(self):
        return self._single().name

    @property
    def index(self):
        return self._single().index

    @property
    def data_type(self):
        return self._single().data_type

    def values(self):
        return self._single().values()

    def values_distinct(self):
        return self._single().values_distinct()

    def values_without_nulls(self):
        return self._single().values_without_nulls()


def column_labels(table):
    return tuple(origin.label for origin in get_origins(table))


def label_map(table):
    """Return ``label -> position`` for a table (synthesized if necessary)."""
    return get_origins(table).positions


def matching_columns(table, label):
    """Return every column carrying an original SQL ``label``."""
    return columns_by_label(table).get(label, ColumnGroup())


def columns_by_label(table):
    """Return ``label -> ColumnGroup`` for the table's original SQL labels."""
    grouped = OrderedDict()
    for origin in get_origins(table):
        grouped.setdefault(origin.label, []).append(table.columns[origin.index])
    return OrderedDict((label, ColumnGroup(columns)) for label, columns in grouped.items())
