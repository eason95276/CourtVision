"""Production retraining only: reuse frozen experiment choices, no new tuning."""
import json
from .prediction import latest
from .features import history,dataset
from .experiment import train_production,configurations
from .prediction import local_day
from .storage import now,digest
from . import ROOT


def run():
    reference=latest('experiments','experiment.json')
    experiment=json.loads(reference.read_text(encoding='utf8'))
    games,stats=history()
    cutoff=now();games=games[games.game_date.dt.normalize()<local_day(cutoff)]
    stats=stats[stats.game_id.isin(games.game_id)]
    frame,groups,_=dataset(games,stats)
    experiment['frozen_selection_reference']=str(reference)
    experiment['experiment_source_hashes']=experiment['source_hashes']
    experiment['source_hashes']={str(p.relative_to(ROOT)):digest(p) for p in [ROOT/'data/raw/Games.csv',ROOT/'data/raw/TeamStatistics.csv']}
    experiment['production_requested_cutoff']=cutoff
    return train_production(frame,configurations(groups),experiment['selection'],experiment)

if __name__=='__main__':print(run())
