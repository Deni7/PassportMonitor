from app.domain import normalize

# Explicit aliases from discovered catalogs. Unknown services retain their exact title.
# Both providers have combined queues supporting either passport choice.
DMSU_COMBINED = "Паспорт громадянина України для виїзду за кордон, або у формі картки (ID)"
ALIASES = {
    "passport": {
        "dmsu": {"Оформлення паспорта громадянина України для виїзду за кордон", DMSU_COMBINED},
        "document": {"Закордонний паспорт та (або) ID-картка"},
    },
    "id_card": {
        "dmsu": {"Оформлення паспорта громадянина України у вигляді ID картки", DMSU_COMBINED},
        "document": {"Закордонний паспорт та (або) ID-картка"},
    },
}
TITLES = {"passport": "Оформлення закордонного паспорта", "id_card": "Оформлення ID-картки"}


def canonical_services(provider, service):
    matched = [
        key
        for key, providers in ALIASES.items()
        if normalize(service.name) in {normalize(n) for n in providers.get(provider, set())}
    ]
    return matched or [f"{provider}:{service.id}:{normalize(service.name)}"]


def service_matches(provider, service, canonical):
    return canonical in canonical_services(provider, service)
