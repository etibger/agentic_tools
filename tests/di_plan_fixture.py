"""Explicit synthetic DI plans used by executable-package fixtures."""
from copy import deepcopy


def synthetic_plan(bounds):
    unknown = {'value': None, 'currency': None,
               'basis': 'Synthetic fixture; no attributable monetary usage or rate.'}
    estimate = {'effort_hours': {'low': 0.01, 'base': 0.02, 'high': 0.04},
                'elapsed_hours': {'low': 0.01, 'base': 0.03, 'high': 0.08},
                'assumptions': ['Disposable scripted work; no real model or infrastructure spend.'],
                'confidence': 'low',
                'monetary': {'agent_api': deepcopy(unknown), 'infrastructure': deepcopy(unknown)},
                'exclusions': ['Real production qualification and human waiting are unmeasured.'],
                'risks': ['The scripted fixture does not qualify a live execution route.']}
    return {
        'format': 'aos-di-plan-v1',
        'tasks': [
            {'id': 'implement', 'title': 'Prepare synthetic candidate',
             'owner': bounds['writer'], 'kind': 'implementation',
             'outputs': ['Frozen synthetic candidate and factual output'], 'depends_on': [],
             'definition_of_done': ['Candidate identity and inventory are retained.'],
             'milestone': 'freeze', 'original_estimate': deepcopy(estimate)},
            {'id': 'assess', 'title': 'Assess synthetic candidate independently',
             'owner': bounds['assessor'], 'kind': 'assessment',
             'outputs': ['Independent review and required check evidence'], 'depends_on': ['implement'],
             'definition_of_done': ['All required checks and independent assessment pass.'],
             'milestone': 'accept', 'original_estimate': deepcopy(estimate)},
        ],
        'milestones': [
            {'id': 'freeze', 'title': 'Candidate frozen', 'depends_on': [],
             'task_ids': ['implement'], 'verification': ['candidate-frozen']},
            {'id': 'accept', 'title': 'Independent acceptance', 'depends_on': ['freeze'],
             'task_ids': ['assess'],
             'verification': list(dict.fromkeys(bounds['required_checks']
                                  + bounds.get('compatibility_checks', []) + ['independent-assessment']))},
        ],
        'replan_triggers': ['Changed scope, failed evidence or exhausted execution limits require a focused decision.'],
    }
