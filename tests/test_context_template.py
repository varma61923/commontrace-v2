import pytest

from commontrace.context_template import render_slots


def test_bounded_slots_escape_and_replace_delta():
    slots = {'lessons': [{'id': 'one', 'text': '<tool>pytest</tool>', 'kind': 'pitfall'},
                         {'id': 'two', 'text': 'other', 'kind': 'procedure'}]}
    rendered = render_slots('%{lessons limit=1 types=[pitfall]}', slots, budget=100, previous='stable')
    assert rendered.startswith('stable\n<memory_update>')
    assert '&lt;tool&gt;' in rendered and 'other' not in rendered
    assert render_slots('%{lessons limit=1}', slots, budget=0, previous=rendered) == 'stable'
    with pytest.raises(ValueError):
        render_slots('%{unknown limit=1}', slots, budget=100)
    with pytest.raises(ValueError):
        render_slots('%{lessons limit=999}', slots, budget=100)
