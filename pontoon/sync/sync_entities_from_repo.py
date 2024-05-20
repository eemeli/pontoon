import logging
from datetime import datetime
from os.path import isfile, relpath
from typing import cast

from django.db import transaction
from django.db.models import BaseManager
from moz.l10n.paths import L10nConfigPaths, L10nDiscoverPaths

from pontoon.base.models import Entity, Locale, Project, Resource, TranslatedResource
from pontoon.sync.exceptions import ParseError
from pontoon.sync.formats import parse
from pontoon.sync.formats.silme import SilmeEntity, SilmeResource  # Approximate types

log = logging.getLogger(__name__)


def sync_entities_from_repo(
    project: Project,
    locale_map: dict[str, Locale],
    paths: L10nConfigPaths | L10nDiscoverPaths,
    ref_root: str,
    changed: list[str],
    removed: list[str],
    now: datetime,
) -> tuple[list[str], list[str], list[str]]:
    # db_path -> parsed_resource
    updates: dict[str, SilmeResource | None] = {}
    source_locale = Locale.objects.get(code="en-US")
    for path in changed:
        db_path = relpath(path[:-1] if path.endswith(".pot") else path, ref_root)
        try:
            res = parse(path, locale=source_locale)
        except ParseError as error:
            log.error(f"Skipping resource {path} due to ParseError: {error}")
            res = None
        updates[db_path] = res

    with transaction.atomic():
        removed_paths = remove_resources(project.resources, ref_root, removed)
        changed_paths = update_resources(project.resources, updates, now)
        added_paths = add_resources(
            project, locale_map, paths, updates, changed_paths, now
        )

    return added_paths, changed_paths, removed_paths


def remove_resources(
    resources: BaseManager[Resource], ref_root: str, remove: list[str]
) -> list[str]:
    if not remove:
        return []
    removed_paths = [
        path[:-1] if path.endswith(".pot") else path
        for path in (relpath(path, ref_root) for path in remove)
    ]
    removed_resources = resources.filter(path__in=removed_paths)
    removed_paths = list(removed_resources.values_list("path", flat=True))
    if removed_paths:
        log.debug(f"Removed files: {', '.join(removed_paths)}")
        # FIXME: https://github.com/mozilla/pontoon/issues/2133
        removed_resources.delete()
    return removed_paths


def update_resources(
    resources: BaseManager[Resource],
    updates: dict[str, SilmeResource | None],
    now: datetime,
) -> list[str]:
    if not updates:
        return []
    changed_resources = resources.filter(path__in=updates.keys())
    for cr in changed_resources:
        cr.total_strings = len(updates[cr.path].entities)
    Resource.objects.bulk_update(changed_resources, ("total_strings"))

    prev_entities = {
        (e.resource.path, e.key or e.string): e
        for e in Entity.objects.filter(
            resource__in=changed_resources, obsolete=False
        ).select_related("resource")
    }
    next_entities = {
        (path, entity.key or entity.string): entity
        for path, entity in (
            (cr.path, entity_from_source(cr, now, 0, tx))
            for cr in changed_resources
            for tx in updates[cr.path].translations
        )
    }

    obsolete_entities = [
        ent
        for key, ent in prev_entities.items()
        if key in prev_entities.keys() - next_entities.keys()
    ]
    for ent in obsolete_entities:
        ent.obsolete = True
        ent.date_obsoleted = now
    obs_count = Entity.objects.bulk_update(
        obsolete_entities, ("obsolete", "date_obsoleted")
    )

    mod_count = Entity.objects.bulk_update(
        (
            ent
            for key, ent in next_entities.items()
            if key in prev_entities.keys() & next_entities.keys()
            and not entities_same(ent, prev_entities[key])
        ),
        (
            "string",
            "string_plural",
            "comment",
            "source",
            "group_comment",
            "resource_comment",
            "context",
        ),
    )

    # FIXME: Entity order should be updated on insertion
    added_entities = Entity.objects.bulk_create(
        ent
        for key, ent in next_entities.items()
        if key in next_entities.keys() - prev_entities.keys()
    )

    log.debug(
        f"Updated entities: added {len(added_entities)}, changed {mod_count}, obsoleted {obs_count}"
    )
    return list(changed_resources.values_list("path", flat=True))


def add_resources(
    project: Project,
    locale_map: dict[str, Locale],
    paths: L10nConfigPaths | L10nDiscoverPaths,
    updates: dict[str, SilmeResource | None],
    changed_paths: list[str],
    now: datetime,
) -> list[str]:
    added_resources = [
        Resource(
            path=db_path,
            format=Resource.get_path_format(db_path),
            total_strings=len(res.entities),
        )
        for db_path, res in updates.items()
        if res is not None and db_path not in changed_paths
    ]
    if not added_resources:
        return []

    added_resources = Resource.objects.bulk_create(added_resources)
    ordered_resources = cast(BaseManager[Resource], project.resources).order_by("path")
    for idx, r in enumerate(ordered_resources):
        r.order = idx
    Resource.objects.bulk_update(ordered_resources, ["order"])

    Entity.objects.bulk_create(
        (
            entity_from_source(resource, now, idx, tx)
            for resource in added_resources
            for idx, tx in enumerate(updates[resource.path].translations)
        )
    )

    def is_translated_resource(resource: Resource, locale_code: str) -> bool:
        if locale_code not in locale_map:
            return False
        if resource.format in {"po", "xliff"}:
            # For bilingual formats, only create TranslatedResource
            # if the resource exists for the locale.
            target_path = paths.target_path(resource.path, locale_code)
            return target_path is not None and isfile(target_path)
        return True

    TranslatedResource.objects.bulk_create(
        (
            TranslatedResource(
                resource=resource,
                locale=locale_map[locale_code],
                total_strings=resource.total_strings,
            )
            for resource in added_resources
            for locale_code in paths.target_locales(resource.path)
            if is_translated_resource(resource, locale_code)
        )
    )

    added_paths = [ar.path for ar in added_resources]
    log.debug(f"Added files: {', '.join(added_paths)}")
    return added_paths


def entity_from_source(
    resource: Resource, now: datetime, idx: int, tx: SilmeEntity
) -> Entity:
    return Entity(
        string=tx.source_string,
        string_plural=tx.source_string_plural,
        key=tx.key,
        comment="\n".join(tx.comments),
        order=tx.order or idx,
        source=tx.source,
        resource=resource,
        date_created=now,
        group_comment="\n".join(
            tx.group_comments if hasattr(tx, "group_comments") else None
        ),
        resource_comment="\n".join(
            tx.resource_comments if hasattr(tx, "resource_comments") else None
        ),
        context=tx.context,
    )


def entities_same(a: Entity, b: Entity) -> bool:
    return (
        a.string == b.string
        and a.string_plural == b.string_plural
        and a.comment == b.comment
        and a.source == b.source
        and a.group_comment == b.group_comment
        and a.resource_comment == b.resource_comment
        and a.context == b.context
    )
