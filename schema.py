"""Feed and selection payload validation against a renderer's schema.

Single concept: checking caller-supplied values against the declared param and
input schemas, raising ValueError naming the defect. Unknown object keys and
unknown params pass (forward compatibility).
"""

INPUT_TYPES = {"string", "integer", "number", "boolean", "object", "array"}


def _type_ok(value, typename):
    if typename == "string":
        return isinstance(value, str)
    if typename == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typename == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typename == "boolean":
        return isinstance(value, bool)
    if typename == "object":
        return isinstance(value, dict)
    if typename == "array":
        return isinstance(value, (list, tuple))
    return False


def validate_value(value, spec, where="value"):
    """Validate a feed payload (or a selection param) against one schema
    spec. Raises ValueError on mismatch. Unknown object keys are allowed so
    enriched payloads keep passing when the sender adds a field."""
    if not isinstance(spec, dict):
        raise ValueError("%s: bad schema spec" % where)
    typename = spec.get("type", "string")
    if typename not in INPUT_TYPES:
        raise ValueError("%s: unknown type %r" % (where, typename))
    if not _type_ok(value, typename):
        raise ValueError("%s: expected %s, got %s"
                         % (where, typename, type(value).__name__))
    if typename == "object":
        for key in spec.get("required") or []:
            if key not in value:
                raise ValueError("%s: missing required field %r" % (where, key))
        for key, subspec in (spec.get("properties") or {}).items():
            if key in value:
                validate_value(value[key], subspec, "%s.%s" % (where, key))
    if typename == "array" and "items" in spec:
        for i, item in enumerate(value):
            validate_value(item, spec["items"], "%s[%d]" % (where, i))
    if typename == "string" and "maxLength" in spec:
        try:
            cap = int(spec["maxLength"])
        except (TypeError, ValueError):
            cap = -1
        if cap >= 0 and len(value) > cap:
            raise ValueError("%s: string length %d over cap %d"
                             % (where, len(value), cap))


def validate_params(params, schema):
    """Selection-time check: required params present, supplied params typed.
    Unknown params are ignored (forward compatibility)."""
    params = params or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("required") and key not in params:
            raise ValueError("missing required param %r" % key)
        if key in params:
            validate_value(params[key], spec, "param %r" % key)
    return params
