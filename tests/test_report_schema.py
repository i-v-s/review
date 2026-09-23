import copy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from review_service.models import Finding, GeneratedReport, ReviewItem
from review_service.report_schema import report_schema


def example_report():
    report = GeneratedReport(summary='Итог', items=[ReviewItem(
        section='Решения', title='Решение', explanation='Объяснение', rationale_kind='recorded',
        source_ids=['s1'], fragment_ids=['f1'], dependencies=['f2'],
    )], findings=[Finding(title='Ошибка', description='Описание', source_ids=['s2'], fragment_ids=['f2'])]).model_dump()
    for item in [*report['items'], *report['findings']]:
        item.pop('id')
        item.pop('reviewed', None)
    return report


def test_schema_accepts_full_report_and_preserves_field_order():
    schema = report_schema({'s1', 's2'}, {'f1', 'f2'})
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example_report())
    assert list(schema['properties']) == ['summary', 'items', 'findings']
    assert GeneratedReport.model_validate(example_report()).items[0].title == 'Решение'


@pytest.mark.parametrize('kind,field', [
    ('items', 'source_ids'), ('items', 'fragment_ids'), ('items', 'dependencies'),
    ('findings', 'source_ids'), ('findings', 'fragment_ids'),
])
def test_schema_rejects_references_outside_this_request(kind, field):
    validator = Draft202012Validator(report_schema({'s1', 's2'}, {'f1', 'f2'}))
    report = example_report()
    report[kind][0][field] = ['not-in-this-batch']
    with pytest.raises(ValidationError):
        validator.validate(report)


def test_no_sources_only_allows_empty_references_and_unrecorded_rationale():
    schema = report_schema(set(), {'f1', 'f2'})
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    report = example_report()
    for item in [*report['items'], *report['findings']]:
        item['source_ids'] = []
    report['items'][0]['rationale_kind'] = 'unknown'
    validator.validate(report)
    for kind in ('items', 'findings'):
        invalid = copy.deepcopy(report)
        invalid[kind][0]['source_ids'] = ['s1']
        with pytest.raises(ValidationError):
            validator.validate(invalid)
    report['items'][0]['rationale_kind'] = 'recorded'
    with pytest.raises(ValidationError):
        validator.validate(report)


@pytest.mark.parametrize('field,value', [('id', 'invented'), ('reviewed', True), ('extra', 'field')])
def test_model_cannot_generate_service_fields_or_extra_properties(field, value):
    validator = Draft202012Validator(report_schema({'s1', 's2'}, {'f1', 'f2'}))
    report = example_report()
    report['items'][0][field] = value
    with pytest.raises(ValidationError):
        validator.validate(report)


def test_all_output_fields_are_required_and_schemas_do_not_share_mutable_state():
    validator = Draft202012Validator(report_schema({'s1', 's2'}, {'f1', 'f2'}))
    original = example_report()
    for location in (None, 'items', 'findings'):
        fields = original if location is None else original[location][0]
        for field in fields:
            report = copy.deepcopy(original)
            target = report if location is None else report[location][0]
            del target[field]
            with pytest.raises(ValidationError):
                validator.validate(report)
    empty = report_schema(set(), set())
    later = report_schema({'new-source'}, {'new-fragment'})
    assert empty['$defs']['ReviewItem']['properties']['source_ids']['maxItems'] == 0
    assert later['$defs']['ReviewItem']['properties']['source_ids']['items']['enum'] == ['new-source']
    assert 'maxItems' not in later['$defs']['ReviewItem']['properties']['source_ids']
