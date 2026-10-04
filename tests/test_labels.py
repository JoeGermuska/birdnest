"""Labels: original labels from MusicBrainz (enrich_mb_labels.py), crediting plays, label pages and the ranking."""
import pytest

import enrich_mb_labels
import factoids

ATLANTIC = {'id': 'a', 'name': 'Atlantic'}


def rel(date, country, label=ATLANTIC, status='Official'):
    return {'id': f'{date}-{country}', 'date': date, 'country': country, 'status': status,
            'label-info': [{'label': label, 'catalog-number': 'X1'}] if label else []}


def test_earliest_labeled_takes_the_first_year():
    r, li = enrich_mb_labels.earliest_labeled([rel('1996', 'GB', {'id': 'b', 'name': 'Sequel'}), rel('1968', 'US')], 'US')
    assert li['label']['name'] == 'Atlantic' and r['date'] == '1968'


def test_earliest_labeled_prefers_home_and_precise_dates():
    sussex = {'id': 's', 'name': 'Sussex'}
    releases = [rel('1971', 'IT', {'id': 'am', 'name': 'A&M'}), rel('1971-05', 'US', sussex)]
    assert enrich_mb_labels.earliest_labeled(releases, 'US')[1]['label']['name'] == 'Sussex'
    assert enrich_mb_labels.earliest_labeled(releases, None)[1]['label']['name'] == 'Sussex'  # the precise date


def test_earliest_labeled_never_reaches_past_the_first_year():
    unlabeled = rel('1969', 'US', label=None)
    no_label = rel('1969-03', 'US', {'id': enrich_mb_labels.NO_LABEL, 'name': '[no label]'})
    assert enrich_mb_labels.earliest_labeled([unlabeled, no_label, rel('1999', 'US')], 'US') == (None, None)
    assert enrich_mb_labels.earliest_labeled([rel('1970', 'US', status='Bootleg')], 'US')[1]  # no official: any status


def test_label_key():
    assert factoids._label_key('Numero Group') == factoids._label_key('numero group')
    assert factoids._label_key("Honest Jon's Records") == factoids._label_key('Honest Jon’s Records')
    assert factoids._label_key('Thrill Jockey Records') == factoids._label_key('Thrill Jockey')


@pytest.fixture(scope='module')
def history(app_module):
    return factoids.get_history()


def test_credited_plays_add_up(history):
    if not history.label_plays:
        pytest.skip('no MusicBrainz labels in this database')
    assert sum(history.label_plays.values()) <= history.total_track_plays
    for aid, mbid in history.album_label.items():  # credited through Spotify's name: never a reissue arm and the like
        if history.album_label_how[aid] == 'spotify':
            assert history.labels[mbid]['type'] not in factoids.NOT_RELEASING_TYPES


def test_label_page_and_ranking(app_module, history):
    if not history.label_plays:
        pytest.skip('no MusicBrainz labels in this database')
    client = app_module.app.test_client()
    top = max(history.label_plays, key=history.label_plays.get)
    page = client.get(f'/label/{top}')
    assert page.status_code == 200 and f'/radio?label={top}' in page.get_data(as_text=True)
    assert client.get('/label/not-a-label').status_code == 404
    table = history.ranking('labels')
    assert table['rows'][0]['anchor'] == top
