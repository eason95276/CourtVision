"""Only delta: decimal-exact JSON Schema multipleOf; same schema and semantics."""
from decimal import Decimal,InvalidOperation
from jsonschema import Draft202012Validator,validators,ValidationError
from evaluation.candidate_v3 import contracts_v3 as v3

def multiple_of_decimal(validator,divisor,instance,schema):
    if not validator.is_type(instance,'number'):return
    try:
        value=Decimal(str(instance));unit=Decimal(str(divisor))
        if not value.is_finite() or unit<=0 or value%unit!=0:yield ValidationError(str(instance)+' is not a decimal-exact multiple of '+str(divisor))
    except InvalidOperation:yield ValidationError('non-finite or invalid decimal')

ExactValidator=validators.extend(Draft202012Validator,{'multipleOf':multiple_of_decimal})
schema=v3.schema
normalize=v3.normalize
canonical_semantics=v3.canonical_semantics

def validate_schema(output,context):ExactValidator(schema(context)).validate(output)

def validate(output,context):
    validate_schema(output,context)
    return v3.old.validate_historical(output,context)

def preserve(original,repaired,context):
    a=canonical_semantics(original,context);z=canonical_semantics(repaired,context)
    v3.old.preserve_semantics(a,z,context);validate(z,context);return True
