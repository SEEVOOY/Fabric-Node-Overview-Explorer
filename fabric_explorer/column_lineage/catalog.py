"""What a piece of Spark code does to a column, in a handful of categories.

Every definition node in the column lineage gets one or more categories. Each category has a
letter (shown in the node's balloon, like the function letters on an instrument tag in a process
diagram) and a scope:

    column  changes the values of the columns that pass through it (upper, cast, when, sum, ...)
    row     changes which rows there are; every column passing through goes through it
            (filter, join, group by, merge, ...)

FUNCTIONS maps Spark SQL and pyspark.sql.functions names (lower case) to a category. COLUMN_METHODS
does the same for pyspark Column methods (and the pandas methods that mean the same). LITERAL_ARGS
says which positional arguments of a pyspark function are plain values rather than column names,
since pyspark accepts a column name as a string almost everywhere (F.upper("TagNo")), but not in
those positions (F.date_format("Created", "yyyy-MM"), F.concat_ws("-", "a", "b")).

The colours live in the page's stylesheet (--cat-<letter>), so both themes can tune them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

COLUMN, ROW = "column", "row"


@dataclass(frozen=True)
class Category:
    key: str
    letter: str
    name: str
    scope: str
    description: str
    examples: str


CATEGORIES = {c.key: c for c in (
    Category("text", "S", "Text", COLUMN, "Changes text", "upper, trim, concat, substring, regexp_replace, split"),
    Category("number", "N", "Number", COLUMN, "Calculates with numbers", "+ - * /, round, abs, greatest, pow"),
    Category("date", "D", "Date and time", COLUMN, "Works with dates and times",
             "to_date, date_format, current_timestamp, datediff, year"),
    Category("condition", "C", "Condition and nulls", COLUMN, "Chooses a value, compares or fills in missing values",
             "when / otherwise, CASE, coalesce, isNull, isin, =, AND"),
    Category("cast", "T", "Type change", COLUMN, "Changes the data type", "cast, astype, to_number, int(), string()"),
    Category("aggregate", "A", "Aggregate", COLUMN, "Summarises many rows into one value",
             "sum, count, avg, max, collect_list"),
    Category("window", "W", "Window", COLUMN, "Looks at neighbouring rows", "row_number, rank, lag, lead, over"),
    Category("structure", "X", "Arrays, maps and JSON", COLUMN, "Builds or takes apart nested data",
             "explode, struct, from_json, get_json_object, element_at"),
    Category("hash", "H", "Hash and IDs", COLUMN, "Makes keys, hashes and IDs",
             "md5, sha2, xxhash64, uuid, monotonically_increasing_id"),
    Category("constant", "L", "Constant", COLUMN, "Sets a fixed value", "lit, 'text', 42"),
    Category("rename", "R", "Rename", COLUMN, "Gives a column another name", "alias, withColumnRenamed, AS"),
    Category("custom", "U", "Own code", COLUMN, "Code the app can't see into: a UDF or another function",
             "udf, pandas_udf, a helper function"),
    Category("filter", "F", "Filter rows", ROW, "Keeps some rows and drops the rest",
             "filter, where, dropDuplicates, distinct, limit, dropna"),
    Category("join", "J", "Join and union", ROW, "Combines rows from two datasets",
             "join, union, unionByName, intersect"),
    Category("group", "G", "Group rows", ROW, "Groups rows, one row per group", "groupBy, GROUP BY, pivot"),
    Category("merge", "M", "Merge (upsert)", ROW, "Updates and inserts rows in a Delta table",
             "MERGE INTO, DeltaTable.merge, UPDATE"),
    Category("copy", "P", "Copy (pipeline)", ROW, "A pipeline's Copy activity moves the rows",
             "Copy activity, its column mapping"),
)}

ROW_CATEGORIES = frozenset(k for k, c in CATEGORIES.items() if c.scope == ROW)


def _names(category: str, text: str) -> dict:
    return {n: category for n in text.split()}


FUNCTIONS: dict[str, str] = {
    **_names("text", """
        upper lower ucase lcase initcap trim ltrim rtrim btrim concat concat_ws substring substr substring_index
        left right lpad rpad repeat reverse replace translate regexp_replace regexp_extract regexp_extract_all
        regexp_substr regexp_count regexp_instr regexp_like regexp rlike like ilike split split_part length
        char_length character_length len octet_length bit_length instr locate position format_string printf
        format_number overlay soundex levenshtein ascii chr char base64 unbase64 encode decode sentences contains
        startswith endswith elt find_in_set quote mask url_encode url_decode parse_url space collate strip
        lstrip rstrip title capitalize zfill"""),
    **_names("number", """
        abs round bround ceil ceiling floor sqrt cbrt exp expm1 log log10 log2 log1p ln pow power mod pmod div
        greatest least sign signum negative negate positive factorial hypot sin cos tan asin acos atan atan2 sinh
        cosh tanh degrees radians pi e rand randn random try_add try_subtract try_multiply try_divide try_mod
        width_bucket conv hex unhex bin shiftleft shiftright shiftrightunsigned bitwise_not bit_count bit_get rint
        + - * / % ** clip"""),
    **_names("date", """
        current_date current_timestamp now localtimestamp current_timezone curdate to_date to_timestamp
        to_timestamp_ntz to_timestamp_ltz try_to_timestamp date_format year years month months day days dayofmonth
        dayofweek dayofyear weekday weekofyear quarter hour hours minute second date_add dateadd date_sub datediff
        date_diff timestampadd timestampdiff add_months months_between last_day next_day trunc date_trunc
        from_unixtime unix_timestamp to_unix_timestamp unix_date unix_seconds unix_millis unix_micros
        timestamp_seconds timestamp_millis timestamp_micros date_from_unix_date from_utc_timestamp to_utc_timestamp
        make_date make_timestamp make_timestamp_ntz make_timestamp_ltz make_interval make_dt_interval
        make_ym_interval extract date_part datepart window window_time session_window convert_timezone dayname
        monthname to_datetime tz_localize tz_convert"""),
    **_names("condition", """
        when otherwise case if iff coalesce nvl nvl2 ifnull nullif nullifzero zeroifnull isnull isnotnull isnan
        nanvl equal_null assert_true raise_error isin between not and or compare fillna fill_null notnull isna
        notna"""),
    **_names("cast", """
        cast try_cast astype to_number try_to_number to_char to_varchar to_binary try_to_binary string int integer
        bigint smallint tinyint double float decimal boolean date timestamp binary to_numeric"""),
    **_names("aggregate", """
        sum sum_distinct sumdistinct count count_distinct countdistinct approx_count_distinct approxcountdistinct
        avg mean min max max_by min_by first last any_value collect_list collect_set array_agg stddev stddev_pop
        stddev_samp std variance var_pop var_samp percentile percentile_approx approx_percentile median mode
        kurtosis skewness corr covar_pop covar_samp bool_and bool_or every some any count_if bit_and bit_or
        bit_xor listagg string_agg grouping grouping_id histogram_numeric nunique size_agg"""),
    **_names("window", """
        row_number rank dense_rank percent_rank cume_dist ntile lag lead nth_value first_value last_value over
        cumsum cumcount shift rolling"""),
    **_names("structure", """
        explode explode_outer posexplode posexplode_outer inline inline_outer array array_contains array_distinct
        array_except array_intersect array_join array_max array_min array_position array_remove array_repeat
        array_sort array_union array_append array_prepend array_insert array_compact arrays_overlap arrays_zip
        element_at try_element_at size cardinality slice sort_array sequence shuffle flatten struct named_struct
        map create_map map_from_arrays map_from_entries map_concat map_keys map_values map_entries map_filter
        map_zip_with str_to_map transform filter exists forall aggregate reduce zip_with from_json to_json
        get_json_object json_tuple schema_of_json json_array_length json_object_keys from_csv to_csv
        schema_of_csv parse_json get getitem getfield withfield dropfields"""),
    **_names("hash", """
        md5 sha sha1 sha2 crc32 hash xxhash64 uuid monotonically_increasing_id spark_partition_id
        input_file_name input_file_block_length input_file_block_start current_user user session_user
        current_database current_catalog current_schema version aes_encrypt aes_decrypt try_aes_decrypt"""),
    **_names("constant", "lit typedlit"),
}

# pyspark Column methods (and pandas Series methods with the same meaning) -> function name
COLUMN_METHODS: dict[str, str] = {
    "cast": "cast", "astype": "astype", "try_cast": "try_cast",
    "isNull": "isnull", "isNotNull": "isnotnull", "isNaN": "isnan", "isin": "isin", "between": "between",
    "eqNullSafe": "equal_null", "otherwise": "otherwise", "when": "when",
    "like": "like", "rlike": "rlike", "ilike": "ilike", "contains": "contains", "startswith": "startswith",
    "endswith": "endswith", "substr": "substr",
    "getItem": "getitem", "getField": "getfield", "withField": "withfield", "dropFields": "dropfields",
    "over": "over", "bitwiseAND": "&", "bitwiseOR": "|", "bitwiseXOR": "^",
    # pandas
    "fillna": "fillna", "isna": "isna", "isnull": "isnull", "notna": "notna", "notnull": "notnull",
    "round": "round", "abs": "abs", "clip": "clip", "upper": "upper", "lower": "lower", "strip": "strip",
    "lstrip": "lstrip", "rstrip": "rstrip", "replace": "replace", "split": "split", "title": "title",
    "capitalize": "capitalize", "zfill": "zfill", "len": "len", "cumsum": "cumsum", "shift": "shift",
    "apply": "apply", "map": "map", "tz_localize": "tz_localize", "tz_convert": "tz_convert",
}
# Column methods that only change how a column is sorted, or reach a pandas accessor: not a definition
IGNORED_METHODS = {"asc", "desc", "asc_nulls_first", "asc_nulls_last", "desc_nulls_first", "desc_nulls_last",
                   "str", "dt", "values", "copy"}
# pandas methods whose argument is code the app can't see into
CUSTOM_METHODS = {"apply", "map", "applymap", "transform", "pipe"}

# pyspark.sql.functions: positional arguments that are values, not column names (0-based; "*" = all).
# (Column methods and operators always take a Python string as a value: F.col("a") == "x".)
LITERAL_ARGS: dict[str, frozenset] = {name: frozenset(pos) for name, pos in {
    "lit": {"*"}, "typedlit": {"*"}, "concat_ws": {0}, "date_format": {1}, "to_date": {1}, "to_timestamp": {1},
    "to_timestamp_ntz": {1}, "to_timestamp_ltz": {1}, "unix_timestamp": {1}, "from_unixtime": {1},
    "to_unix_timestamp": {1}, "date_trunc": {0}, "trunc": {1}, "regexp_replace": {1, 2}, "regexp_extract": {1, 2},
    "regexp_extract_all": {1, 2}, "regexp_like": {1}, "regexp": {1}, "rlike": {1}, "regexp_count": {1},
    "regexp_substr": {1}, "regexp_instr": {1}, "split": {1, 2}, "split_part": {1, 2}, "substring": {1, 2},
    "substr": {1, 2}, "substring_index": {1, 2}, "left": {1}, "right": {1}, "lpad": {1, 2}, "rpad": {1, 2},
    "repeat": {1}, "translate": {1, 2}, "replace": {1, 2}, "instr": {1}, "locate": {0, 2}, "format_string": {0},
    "printf": {0}, "format_number": {1}, "round": {1}, "bround": {1}, "conv": {1, 2}, "shiftleft": {1},
    "shiftright": {1}, "date_add": {1}, "date_sub": {1}, "dateadd": {1}, "add_months": {1}, "next_day": {1},
    "months_between": {2}, "from_utc_timestamp": {1}, "to_utc_timestamp": {1}, "convert_timezone": {0, 1},
    "sha2": {1}, "get_json_object": {1}, "json_tuple": {"*"}, "from_json": {1, 2}, "to_json": {1},
    "from_csv": {1, 2}, "element_at": {1}, "array_contains": {1}, "array_remove": {1}, "array_position": {1},
    "array_join": {1, 2}, "slice": {1, 2}, "when": {1}, "ntile": {0}, "lag": {1, 2}, "lead": {1, 2},
    "nth_value": {1}, "percentile_approx": {1, 2}, "percentile": {1}, "window": {1, 2, 3}, "encode": {1},
    "decode": {1}, "to_char": {1}, "to_varchar": {1}, "to_number": {1}, "mask": {1, 2, 3, 4}, "parse_url": {1, 2},
    "make_interval": {"*"}, "expr": {"*"},
}.items()}

# Functions that make a value out of nothing (no input columns): their own category is enough
GENERATORS = {"current_date", "current_timestamp", "now", "localtimestamp", "current_timezone", "curdate",
              "uuid", "monotonically_increasing_id", "spark_partition_id", "input_file_name", "rand", "randn",
              "random", "current_user", "user", "session_user", "current_database", "current_catalog",
              "current_schema", "version", "pi", "e"}

# Spark SQL functions that are written without parentheses
BARE_FUNCTIONS = {"current_date", "current_timestamp", "current_user", "current_timezone", "localtimestamp",
                  "current_database", "current_catalog", "current_schema", "session_user", "user"}

# Spark SQL type names (CAST(x AS <type>), and as conversion functions)
TYPE_NAMES = {"string", "varchar", "char", "int", "integer", "bigint", "long", "smallint", "short", "tinyint",
              "byte", "double", "float", "real", "decimal", "dec", "numeric", "boolean", "bool", "date",
              "timestamp", "timestamp_ntz", "timestamp_ltz", "binary", "array", "map", "struct", "interval", "void",
              "variant"}

OPERATOR_FUNCTIONS = {"+": "+", "-": "-", "*": "*", "/": "/", "%": "%", "**": "**", "||": "concat",
                      "==": "compare", "=": "compare", "!=": "compare", "<>": "compare", "<": "compare",
                      ">": "compare", "<=": "compare", ">=": "compare", "<=>": "equal_null", "&": "and",
                      "|": "or", "~": "not", "&&": "and"}

# Which category wins when an expression mixes several (the first found, in this order, is the main one)
PRIORITY = ["custom", "window", "aggregate", "condition", "structure", "hash", "date", "cast", "text", "number",
            "rename", "constant"]


def category_of(function: str) -> str:
    """The category of one function name (case-insensitive); unknown names are someone's own code."""
    f = function.lower()
    if f in FUNCTIONS:
        return FUNCTIONS[f]
    if f.startswith(("try_", "array_", "map_", "json_", "regexp_", "bit_", "date_", "unix_", "to_timestamp")):
        for prefix, cat in (("try_", "number"), ("array_", "structure"), ("map_", "structure"),
                            ("json_", "structure"), ("regexp_", "text"), ("bit_", "number"), ("date_", "date"),
                            ("unix_", "date"), ("to_timestamp", "date")):
            if f.startswith(prefix):
                return cat
    if f.startswith(("st_", "h3_")):
        return "structure"
    return "custom"


def categories_of(functions) -> list:
    """The categories of a list of functions, the main one first (see PRIORITY), each once."""
    found = []
    for fn in functions:
        cat = category_of(fn)
        if cat not in found:
            found.append(cat)
    return sorted(found, key=lambda c: PRIORITY.index(c) if c in PRIORITY else len(PRIORITY))


def letters(categories) -> str:
    return "".join(CATEGORIES[c].letter for c in categories if c in CATEGORIES)


LEGEND_ORDER = ["text", "number", "date", "condition", "cast", "aggregate", "window", "structure", "hash",
                "constant", "rename", "custom", "filter", "join", "group", "merge", "copy"]

# DAX (semantic model measures and calculated columns), in the same categories
DAX_FUNCTIONS: dict[str, str] = {
    **_names("aggregate", """
        sum sumx average averagex averagea min minx mina max maxx maxa count countx counta countax countrows
        countblank distinctcount distinctcountnoblank median medianx product productx approximatedistinctcount
        percentile.inc percentile.exc percentilex.inc percentilex.exc"""),
    **_names("filter", """
        calculate calculatetable filter all allexcept allselected allnoblankrow removefilters keepfilters values
        distinct selectedvalue hasonevalue hasonefilter isfiltered iscrossfiltered treatas userelationship
        crossfilter"""),
    **_names("condition", """
        if if.eager switch iferror isblank isempty coalesce and or not true false blank isnumber istext iserror
        contains containsrow containsstring"""),
    **_names("date", """
        date datevalue today now year month day hour minute second weekday weeknum eomonth edate datediff
        dateadd datesytd datesmtd datesqtd datesbetween datesinperiod totalytd totalmtd totalqtd
        sameperiodlastyear parallelperiod previousmonth previousyear previousquarter previousday nextmonth
        nextyear startofmonth endofmonth startofyear endofyear calendar calendarauto"""),
    **_names("text", """
        concatenate concatenatex format left right mid upper lower trim len substitute replace search find rept
        exact unichar combinevalues"""),
    **_names("number", "divide round roundup rounddown abs int mod power sqrt exp ln log log10 ceiling floor trunc "
                       "sign rand randbetween fixed"),
    **_names("window", "rankx rank topn window offset index rownumber earlier earliest"),
    **_names("structure", """
        related relatedtable lookupvalue summarize summarizecolumns addcolumns selectcolumns crossjoin union
        intersect except naturalinnerjoin naturalleftouterjoin generate generateall datatable row"""),
    **_names("cast", "value convert currency"),
}


def dax_categories(functions) -> list:
    """The categories of a DAX expression's functions, the main one first (filter last: it shapes context)."""
    found = []
    for fn in functions:
        cat = DAX_FUNCTIONS.get(fn.lower(), "custom")
        if cat not in found:
            found.append(cat)
    order = PRIORITY + ["filter"]
    return sorted(found, key=lambda c: order.index(c) if c in order else len(order))


def catalog_json() -> dict:
    """The categories for the page's legend, column categories first, then row categories."""
    return {"categories": [asdict(CATEGORIES[k]) for k in LEGEND_ORDER]}
