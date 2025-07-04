# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from datetime import timedelta

from flask import flash, session

from indico.core import signals
from indico.core.db.sqlalchemy.protection import make_acl_log_fn
from indico.core.logger import Logger
from indico.core.permissions import ManagementPermission, check_permissions
from indico.core.settings.converters import TimedeltaConverter
from indico.modules.events.contributions.contrib_fields import get_contrib_field_types
from indico.modules.events.contributions.models.contributions import Contribution
from indico.modules.events.contributions.models.fields import ContributionField
from indico.modules.events.models.events import Event, EventType
from indico.modules.events.settings import EventSettingsProxy
from indico.util.i18n import _, ngettext
from indico.web.flask.util import url_for
from indico.web.menu import SideMenuItem


logger = Logger.get('events.contributions')

# Log ACL changes
signals.acl.entry_changed.connect(make_acl_log_fn(Contribution), sender=Contribution, weak=False)


@signals.menu.items.connect_via('event-management-sidemenu')
def _extend_event_management_menu(sender, event, **kwargs):
    if not event.can_manage(session.user, permission='contributions'):
        return
    if event.type == 'conference':
        return SideMenuItem('contributions', _('Contributions'), url_for('contributions.manage_contributions', event),
                            section='organization')


@signals.users.merged.connect
def _merge_users(target, source, **kwargs):
    from indico.modules.events.contributions.models.principals import ContributionPrincipal
    ContributionPrincipal.merge_users(target, source, 'contribution')


@signals.users.registered.connect
@signals.users.email_added.connect
def _convert_email_principals(user, silent=False, **kwargs):
    from indico.modules.events.contributions.models.principals import ContributionPrincipal
    contributions = ContributionPrincipal.replace_email_with_user(user, 'contribution')
    if contributions and not silent:
        num = len(contributions)
        flash(ngettext('You have been granted manager/submission privileges for a contribution.',
                       'You have been granted manager/submission privileges for {} contributions.', num).format(num),
              'info')


@signals.core.get_fields.connect_via(ContributionField)
def _get_fields(sender, **kwargs):
    from . import contrib_fields
    yield contrib_fields.ContribTextField
    yield contrib_fields.ContribSingleChoiceField


@signals.core.app_created.connect
def _check_field_definitions(app, **kwargs):
    # This will raise RuntimeError if the field names are not unique
    get_contrib_field_types()


@signals.event_management.get_cloners.connect
def _get_contribution_cloner(sender, **kwargs):
    from indico.modules.events.contributions import clone
    yield clone.ContributionFieldCloner
    yield clone.ContributionTypeCloner
    yield clone.ContributionCloner


@signals.core.app_created.connect
def _check_permissions(app, **kwargs):
    check_permissions(Contribution)


@signals.acl.get_management_permissions.connect_via(Event)
def _get_event_management_permissions(sender, **kwargs):
    return ContributionsPermission


@signals.acl.get_management_permissions.connect_via(Contribution)
def _get_contrib_management_permissions(sender, **kwargs):
    return SubmitterPermission


class ContributionsPermission(ManagementPermission):
    name = 'contributions'
    friendly_name = _('Contributions')
    description = _('Grants management rights for contributions.')
    user_selectable = True


class SubmitterPermission(ManagementPermission):
    name = 'submit'
    friendly_name = _('Submission')
    description = _('Grants management rights for materials and minutes.')
    user_selectable = True


@signals.event_management.management_url.connect
def _get_event_management_url(event, **kwargs):
    if event.can_manage(session.user, permission='contributions'):
        return url_for('contributions.manage_contributions', event)


@signals.event.sidemenu.connect
def _extend_event_menu(sender, **kwargs):
    from indico.modules.events.contributions.util import user_has_contributions
    from indico.modules.events.layout.util import MenuEntryData

    def _visible_my_contributions(event):
        if not session.user:
            return False
        return user_has_contributions(event, session.user)

    def _visible_list_of_contributions(event):
        published = contribution_settings.get(event, 'published')
        can_manage = event.can_manage(session.user, permission='contributions')
        return (published or can_manage) and Contribution.query.filter(Contribution.event == event).has_rows()

    yield MenuEntryData(title=_('My Contributions'), name='my_contributions', visible=_visible_my_contributions,
                        endpoint='contributions.my_contributions', position=2, parent='my_conference')
    yield MenuEntryData(title=_('Contribution List'), name='contributions', endpoint='contributions.contribution_list',
                        position=4, static_site=True, visible=_visible_list_of_contributions)
    yield MenuEntryData(title=_('Author List'), name='author_index', endpoint='contributions.author_list', position=5,
                        is_enabled=False, static_site=True)
    yield MenuEntryData(title=_('Speaker List'), name='speaker_index', endpoint='contributions.speaker_list',
                        position=6, is_enabled=False, static_site=True)


@signals.event.created.connect
def _event_created(event, **kwargs):
    if event.type_ == EventType.conference:
        contribution_settings.set(event, 'published', False)


contribution_settings = EventSettingsProxy('contributions', {
    'default_duration': timedelta(minutes=20),
    'submitters_can_edit': False,
    'submitters_can_edit_custom': False,
    'published': True
}, converters={
    'default_duration': TimedeltaConverter
})
