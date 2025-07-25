# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from collections import namedtuple
from io import BytesIO
from operator import attrgetter
from urllib.parse import urlsplit
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from flask import flash, jsonify, redirect, render_template, request, session
from itsdangerous import BadSignature
from markupsafe import Markup, escape
from marshmallow import fields
from PIL import Image
from sqlalchemy.orm import joinedload, load_only, subqueryload
from sqlalchemy.orm.exc import StaleDataError
from webargs import validate
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from indico.core import signals
from indico.core.auth import multipass
from indico.core.cache import make_scoped_cache
from indico.core.config import config
from indico.core.db import db
from indico.core.db.sqlalchemy.util.queries import get_n_matching
from indico.core.errors import UserValueError
from indico.core.marshmallow import mm
from indico.core.notifications import make_email, send_email
from indico.modules.admin import RHAdminBase
from indico.modules.auth import Identity
from indico.modules.auth.models.registration_requests import RegistrationRequest
from indico.modules.auth.util import register_user
from indico.modules.categories import Category
from indico.modules.core.settings import social_settings
from indico.modules.events import Event
from indico.modules.events.contributions.models.contributions import Contribution
from indico.modules.events.sessions.models.sessions import Session
from indico.modules.events.util import serialize_event_for_ical
from indico.modules.logs.models.entries import LogKind, UserLogRealm
from indico.modules.users import User, logger, user_management_settings
from indico.modules.users.export_schemas import DataExportRequestSchema
from indico.modules.users.forms import (AdminAccountRegistrationForm, AdminsForm, AdminUserSettingsForm, MergeForm,
                                        SearchForm, UserEmailsForm, UserPreferencesForm)
from indico.modules.users.models.affiliations import Affiliation
from indico.modules.users.models.emails import UserEmail
from indico.modules.users.models.export import DataExportOptions, DataExportRequestState
from indico.modules.users.models.users import ProfilePictureSource, UserTitle
from indico.modules.users.operations import create_user, delete_or_anonymize_user
from indico.modules.users.schemas import (AffiliationSchema, BasicCategorySchema, FavoriteEventSchema,
                                          UserPersonalDataSchema)
from indico.modules.users.util import (get_avatar_url_from_name, get_gravatar_for_user, get_linked_events,
                                       get_mastodon_server_name, get_related_categories, get_suggested_categories,
                                       get_unlisted_events, get_user_by_email, get_user_titles, log_user_update,
                                       merge_users, search_affiliations, search_users, send_avatar, serialize_user,
                                       set_user_avatar)
from indico.modules.users.views import (WPUser, WPUserDashboard, WPUserDataExport, WPUserFavorites, WPUserPersonalData,
                                        WPUserProfilePic, WPUsersAdmin)
from indico.util.date_time import now_utc
from indico.util.i18n import _, force_locale
from indico.util.images import square
from indico.util.marshmallow import HumanizedDate, ModelField, Principal, validate_with_message
from indico.util.signals import values_from_signal
from indico.util.signing import static_secure_serializer
from indico.util.string import make_unique_token, remove_accents
from indico.util.user import make_user_search_token, validate_search_token
from indico.web.args import use_args, use_kwargs
from indico.web.flask.templating import get_template_module
from indico.web.flask.util import send_file, url_for
from indico.web.forms.base import FormDefaults
from indico.web.http_api.metadata import Serializer
from indico.web.rh import RH, RHProtected, allow_signed_url
from indico.web.util import (ExpectedError, is_legacy_signed_url_valid, jsonify_data, jsonify_form, jsonify_template,
                             strip_path_from_url)


IDENTITY_ATTRIBUTES = {'first_name', 'last_name', 'email', 'affiliation', 'full_name'}
UserEntry = namedtuple('UserEntry', IDENTITY_ATTRIBUTES | {'profile_url', 'avatar_url', 'user'})


def get_events_in_categories(category_ids, user, from_, limit=10):
    """Get all the user-accessible events in a given set of categories."""
    tz = session.tzinfo
    # Find events (past and future) which are closest to the current time
    time_delta = now_utc(False) - Event.start_dt
    absolute_time_delta = db.func.abs(db.func.extract('epoch', time_delta))
    _room_strategy = joinedload('own_room')
    _room_strategy.raiseload('*')
    _room_strategy.joinedload('location').load_only('id', 'room_name_format')
    _room_strategy.load_only('id', 'location_id', 'site', 'building', 'floor', 'number', 'verbose_name')
    query = (Event.query
             .filter(~Event.is_deleted,
                     Event.category_chain_overlaps(category_ids),
                     Event.start_dt.astimezone(session.tzinfo) >= from_.astimezone(tz).date())
             .options(joinedload('category').load_only('id', 'title', 'protection_mode'),
                      joinedload('series'),
                      joinedload('label'),
                      _room_strategy,
                      joinedload('own_venue').load_only('id', 'name'),
                      subqueryload('acl_entries'),
                      load_only('id', 'category_id', 'start_dt', 'end_dt', 'title', 'access_key',
                                'protection_mode', 'series_id', 'series_pos', 'series_count',
                                'label_id', 'label_message', 'description', 'own_room_id', 'own_venue_id',
                                'own_room_name', 'own_venue_name'))
             .order_by(absolute_time_delta, Event.id))
    return get_n_matching(query, limit, lambda x: x.can_access(user))


class RHUserBase(RHProtected):
    flash_user_status = True
    allow_system_user = False

    def _process_args(self):
        self.user = session.user
        if not session.user:
            return
        if 'user_id' in request.view_args:
            self.user = User.get(request.view_args['user_id'])
            if self.user is None:
                raise NotFound('This user does not exist')
            elif request.method == 'GET' and not request.is_xhr and self.flash_user_status:
                # Show messages about the user's status if it's a simple GET request
                if self.user.is_deleted:
                    if self.user.merged_into_id is not None:
                        msg = _('This user has been merged into <a href="{url}">another user</a>.')
                        flash(Markup(msg).format(url=url_for(request.endpoint, self.user.merged_into_user)), 'warning')
                    else:
                        flash(_('This user is marked as deleted.'), 'warning')
                if self.user.is_pending:
                    flash(_('This user is marked as pending, i.e. it has been attached to something but never '
                            'logged in.'), 'warning')
        if not self.allow_system_user and self.user.is_system:
            return redirect(url_for('users.user_profile'))

    def _check_access(self):
        RHProtected._check_access(self)
        if not self.user.can_be_modified(session.user):
            raise Forbidden('You cannot modify this user.')


class RHUserDashboard(RHUserBase):
    management_roles = {'conference_creator', 'conference_chair', 'conference_manager', 'session_manager',
                        'session_coordinator', 'contribution_manager'}
    reviewer_roles = {'paper_manager', 'paper_judge', 'paper_content_reviewer', 'paper_layout_reviewer',
                      'contribution_referee', 'contribution_editor', 'contribution_reviewer', 'abstract_reviewer',
                      'track_convener'}
    attendance_roles = {'contributor', 'contribution_submission', 'abstract_submitter', 'abstract_person',
                        'registration_registrant', 'survey_submitter', 'lecture_speaker'}

    def _process(self):
        self.user.settings.set('suggest_categories', True)
        categories = get_related_categories(self.user)
        categories_events = []
        if categories:
            category_ids = {c['categ'].id for c in categories.values()}
            categories_events = get_events_in_categories(category_ids, self.user, now_utc(False))
        from_dt = now_utc(False) - relativedelta(weeks=1, hour=0, minute=0, second=0)
        linked_events = [(event, {'management': bool(roles & self.management_roles),
                                  'reviewing': bool(roles & self.reviewer_roles),
                                  'attendance': bool(roles & self.attendance_roles),
                                  'favorited': 'favorited' in roles})
                         for event, roles in get_linked_events(self.user, from_dt, 10).items()]
        return WPUserDashboard.render_template('dashboard.html', 'dashboard',
                                               user=self.user,
                                               categories=categories,
                                               categories_events=categories_events,
                                               suggested_categories=get_suggested_categories(self.user),
                                               linked_events=linked_events,
                                               unlisted_events=get_unlisted_events(self.user))


@allow_signed_url
class RHExportDashboardICS(RHProtected):
    def _get_user(self):
        return session.user

    @use_kwargs({
        'from_': HumanizedDate(data_key='from', load_default=None),
        'include': fields.List(fields.Str(), load_default=lambda: {'linked', 'categories'}),
        'limit': fields.Integer(load_default=250, validate=lambda v: 0 < v <= 500)
    }, location='query')
    def _process(self, from_, include, limit):
        now = now_utc(False)
        user = self._get_user()
        all_events = set()

        if 'linked' in include:
            _room_strategy = joinedload('own_room')
            _room_strategy.raiseload('*')
            _room_strategy.joinedload('location').load_only('id', 'room_name_format')
            _room_strategy.load_only('id', 'location_id', 'site', 'building', 'floor', 'number', 'verbose_name')
            all_events |= set(get_linked_events(
                user,
                from_,
                limit=limit,
                load_also=('description', 'own_room_id', 'own_venue_id', 'own_room_name', 'own_venue_name'),
                extra_options=(
                    _room_strategy,
                    joinedload('own_venue').load_only('id', 'name'),
                )
            ))

        if 'categories' in include and (categories := get_related_categories(user)):
            category_ids = {c['categ'].id for c in categories.values()}
            cats_from = from_ or (now - relativedelta(months=2, hour=0, minute=0, second=0))
            all_events |= set(get_events_in_categories(category_ids, user, cats_from, limit=limit*10))

        all_events = sorted(all_events, key=lambda e: (abs(now - e.start_dt), e.id))[:limit]

        response = {'results': [serialize_event_for_ical(event) for event in all_events]}
        serializer = Serializer.create('ics')
        return send_file('event.ics', BytesIO(serializer(response)), 'text/calendar')


class RHExportDashboardICSLegacy(RHExportDashboardICS):
    def _get_user(self):
        user = User.get_or_404(request.view_args['user_id'], is_deleted=False)
        if not is_legacy_signed_url_valid(user, request.full_path):
            raise BadRequest('Invalid signature')
        if user.is_blocked:
            raise BadRequest('User blocked')
        return user

    def _check_access(self):
        # disable the usual RHProtected access check; `_get_user` does it all
        pass


class RHPersonalData(RHUserBase):
    allow_system_user = True

    def _process(self):
        titles = get_user_titles()
        user_values = UserPersonalDataSchema().dump(self.user)
        locked_fields = [] if session.user.is_admin else list(multipass.locked_fields)
        current_affiliation = None
        if self.user.affiliation_link:
            current_affiliation = AffiliationSchema().dump(self.user.affiliation_link)
        has_predefined_affiliations = Affiliation.query.filter(~Affiliation.is_deleted).has_rows()
        allow_custom_affiliations = not user_management_settings.get('only_predefined_affiliations')
        return WPUserPersonalData.render_template('personal_data.html', 'personal_data', user=self.user,
                                                  titles=titles, user_values=user_values, locked_fields=locked_fields,
                                                  locked_field_message=multipass.locked_field_message,
                                                  current_affiliation=current_affiliation,
                                                  has_predefined_affiliations=has_predefined_affiliations,
                                                  allow_custom_affiliations=allow_custom_affiliations,
                                                  allow_deletion=config.ALLOW_ADMIN_USER_DELETION)


class RHUserDataExport(RHUserBase):
    """Frontend page for user data exports."""

    def _process(self):
        if req := self.user.data_export_request:
            data = DataExportRequestSchema().dump(req)
        else:
            data = {'state': DataExportRequestState.none.name}
        export_options = [(opt.name, str(opt.title)) for opt in DataExportOptions]
        return WPUserDataExport.render_template('data_export.html', 'data_export', user=self.user,
                                                export_request=data, export_options=export_options)


class RHUserDataExportDownload(RHUserBase):
    """Download the exported user data archive."""

    def _process(self):
        if not self.user.data_export_request or not self.user.data_export_request.file:
            raise NotFound
        logger.info('User data export for %r downloaded by %r', self.user, session.user)
        return self.user.data_export_request.file.send()


class RHUserDataExportAPI(RHUserBase):
    """API to trigger user data exports."""

    @use_kwargs({
        'options': fields.List(fields.Enum(DataExportOptions), validate=validate.Length(min=1)),
        'include_files': fields.Bool(required=True)
    })
    def _process_POST(self, options, include_files):
        from indico.modules.users.tasks import export_user_data
        export_user_data.delay(self.user, options, include_files)
        logger.info('User data export for %r triggered by %r [%s, files=%r]',
                    self.user, session.user, ', '.join(x.name for x in options), include_files)
        return {'state': DataExportRequestState.running.name}


class RHPersonalDataUpdate(RHUserBase):
    allow_system_user = True

    @use_args(UserPersonalDataSchema, partial=True)
    def _process(self, updates):
        logger.info('Profile of user %r updated by %r: %r', self.user, session.user, updates)
        synced_fields = set(updates.pop('synced_fields', self.user.synced_fields))
        if not session.user.is_admin:
            synced_fields |= multipass.locked_fields & self.user.synced_fields
        syncable_fields = {k for k, v in self.user.synced_values.items()
                           if v or k not in ('first_name', 'last_name')}
        # we set this first so these fields are skipped below and only
        # get updated in synchronize_data which will flash a message
        # informing the user about the changes made by the sync
        old_synced_fields = self.user.synced_fields
        self.user.synced_fields = synced_fields & syncable_fields
        changes = {}
        if old_synced_fields != self.user.synced_fields:
            changes['synced_fields'] = (old_synced_fields, self.user.synced_fields)
        for key, value in updates.items():
            old = getattr(self.user, key)
            if key not in self.user.synced_fields and old != value:
                changes[key] = (old, value)
                setattr(self.user, key, value)
        changes.update(self.user.synchronize_data(refresh=True))
        if changes:
            log_user_update(self.user, changes)
        flash(_('Your personal data was successfully updated.'), 'success')
        return '', 204


class RHSearchAffiliations(RH):
    @use_kwargs({'q': fields.String(load_default='')}, location='query')
    def _process(self, q):
        res = search_affiliations(q)
        return AffiliationSchema(many=True).jsonify(res)


class RHProfilePicturePage(RHUserBase):
    """Page to manage the profile picture."""

    def _process(self):
        return WPUserProfilePic.render_template('profile_picture.html', 'profile_picture',
                                                user=self.user, source=self.user.picture_source.name)


class RHProfilePicturePreview(RHUserBase):
    """Preview the different profile pictures.

    This always uses a fresh picture without any caching.
    """

    flash_user_status = False

    @use_kwargs({'source': fields.Enum(ProfilePictureSource)}, location='view_args')
    def _process(self, source):
        if source == ProfilePictureSource.standard:
            first_name = self.user.first_name[0].upper() if self.user.first_name else ''
            avatar = render_template('users/avatar.svg', bg_color=self.user.avatar_bg_color, text=first_name)
            return send_file('avatar.svg', BytesIO(avatar.encode()), mimetype='image/svg+xml',
                             no_cache=True, inline=True, safe=False)
        elif source == ProfilePictureSource.custom:
            metadata = self.user.picture_metadata
            return send_file('avatar.png', BytesIO(self.user.picture), mimetype=metadata['content_type'],
                             no_cache=True, inline=True)
        else:
            gravatar = get_gravatar_for_user(self.user, source == ProfilePictureSource.identicon, size=80)[0]
            return send_file('avatar.png', BytesIO(gravatar), mimetype='image/png')


class RHProfilePictureDisplay(RH):
    """Display the user's profile picture."""

    def _process_args(self):
        self.user = User.get_or_404(request.view_args['user_id'])
        try:
            sig_user_id = static_secure_serializer.loads(request.view_args['signature'],
                                                         salt='user-profile-picture-display')
            if self.user.id != sig_user_id:
                raise NotFound
        except BadSignature:
            raise NotFound

    def _process(self):
        return send_avatar(self.user)


class RHSaveProfilePicture(RHUserBase):
    """Update the user's profile picture."""

    @use_kwargs({
        'source': fields.Enum(ProfilePictureSource, required=True)
    })
    def _process(self, source):
        self.user.picture_source = source

        if source == ProfilePictureSource.standard:
            self.user.picture = None
            self.user.picture_metadata = None
            logger.info('Profile picture of user %s removed by %s', self.user, session.user)
            self.user.log(UserLogRealm.user, LogKind.negative, 'Profile', 'Picture removed', session.user)
            return '', 204

        if source == ProfilePictureSource.custom:
            f = request.files['picture']
            try:
                pic = Image.open(f)
            except OSError:
                raise UserValueError(_('You cannot upload this file as profile picture.'))
            if pic.format.lower() not in {'jpeg', 'png', 'gif', 'webp'}:
                raise UserValueError(_('The file has an invalid format ({format}).').format(format=pic.format))
            if pic.mode not in ('RGB', 'RGBA'):
                pic = pic.convert('RGB')
            pic = square(pic)
            if pic.height > 256:
                pic = pic.resize((256, 256), resample=Image.Resampling.BICUBIC)
            image_bytes = BytesIO()
            pic.save(image_bytes, 'PNG')
            image_bytes.seek(0)
            set_user_avatar(self.user, image_bytes.read(), f.filename)
        else:
            content, lastmod = get_gravatar_for_user(self.user, source == ProfilePictureSource.identicon, 256)
            set_user_avatar(self.user, content, source.name, lastmod)

        logger.info('Profile picture of user %s updated by %s', self.user, session.user)
        self.user.log(UserLogRealm.user, LogKind.change, 'Profile', 'Picture updated', session.user,
                      data={'Source': source.name.title()})
        return '', 204


class RHUserPreferences(RHUserBase):
    def _process(self):
        extra_preferences = [pref(self.user) for pref in values_from_signal(signals.users.preferences.send(self.user))
                             if pref.is_active(self.user)]
        form_class = UserPreferencesForm
        defaults = FormDefaults(**self.user.settings.get_all())
        for pref in extra_preferences:
            form_class = pref.extend_form(form_class)
            pref.extend_defaults(defaults)
        form = form_class(obj=defaults)
        if form.validate_on_submit():
            data = form.data
            for pref in extra_preferences:
                pref.process_form_data(data)
            self.user.settings.set_multi(data)
            session.lang = self.user.settings.get('lang')
            session.timezone = (self.user.settings.get('timezone') if self.user.settings.get('force_timezone')
                                else 'LOCAL')

            server_info = get_mastodon_server_name(self.user.settings.get('mastodon_server_url'))
            if server_info is not None:
                self.user.settings.set('mastodon_server_name', server_info['name'])
            else:
                self.user.settings.delete('mastodon_server_name')

            flash(_('Preferences saved'), 'success')
            return redirect(url_for('.user_preferences'))
        return WPUser.render_template('preferences.html', 'preferences', user=self.user, form=form)


class RHUserPreferencesMarkdownAPI(RHUserBase):
    def _process(self):
        return jsonify(self.user.settings.get('use_markdown_for_minutes'))


class RHUserPreferencesMastodonServer(RHUserBase):
    @use_kwargs({
        'server_url': fields.String(required=True),
    })
    def _process_POST(self, server_url):
        if not social_settings.get('enabled'):
            raise Forbidden('The social share widget is not enabled.')

        url = urlsplit(server_url)
        if url.scheme not in ('http', 'https'):
            raise ExpectedError(_('Invalid URL.'))
        url = strip_path_from_url(server_url)

        server_info = get_mastodon_server_name(url)
        if not server_info:
            raise ExpectedError(_('Invalid Mastodon server URL.'))

        self.user.settings.set('mastodon_server_url', url)
        self.user.settings.set('mastodon_server_name', server_info['name'])
        return jsonify({'name': server_info['name'], 'url': url})


class RHUserFavorites(RHUserBase):
    def _process(self):
        return WPUserFavorites.render_template('favorites.html', 'favorites', user=self.user)


class RHUserFavoritesAPI(RHUserBase):
    @use_kwargs({
        'fav_user': Principal(load_default=None, data_key='identifier')
    }, location='view_args')
    def _process_args(self, fav_user):
        RHUserBase._process_args(self)
        self.fav_user = fav_user

    def _process_GET(self):
        return jsonify(sorted(u.identifier for u in self.user.favorite_users))

    def _process_PUT(self):
        self.user.favorite_users.add(self.fav_user)
        return jsonify(self.user.identifier), 201

    def _process_DELETE(self):
        self.user.favorite_users.discard(self.fav_user)
        return '', 204


class RHUserFavoritesCategoryAPI(RHUserBase):
    def _process_args(self):
        RHUserBase._process_args(self)
        self.category = (
            Category.get_or_404(request.view_args['category_id']) if 'category_id' in request.view_args else None
        )
        self.suggestion = (
            self.user.suggested_categories.filter_by(category=self.category).first()
            if 'category_id' in request.view_args
            else None
        )

    def _process_GET(self):
        return jsonify({d.id: BasicCategorySchema().dump(d) for d in self.user.favorite_categories})

    def _process_PUT(self):
        if self.category not in self.user.favorite_categories:
            if not self.category.can_access(self.user):
                raise Forbidden
            self.user.favorite_categories.add(self.category)
            signals.users.favorite_category_added.send(self.user, category=self.category)
            if self.suggestion:
                self.user.suggested_categories.remove(self.suggestion)
        return jsonify(success=True)

    def _process_DELETE(self):
        if self.category in self.user.favorite_categories:
            self.user.favorite_categories.discard(self.category)
            try:
                db.session.flush()
            except StaleDataError:
                # Deleted in another transaction
                db.session.rollback()
            signals.users.favorite_category_removed.send(self.user, category=self.category)
            suggestion = self.user.suggested_categories.filter_by(category=self.category).first()
            if suggestion:
                self.user.suggested_categories.remove(suggestion)
        return jsonify(success=True)


class RHUserFavoritesEventAPI(RHUserBase):
    def _process_args(self):
        RHUserBase._process_args(self)
        self.event = (
            Event.get_or_404(request.view_args['event_id']) if 'event_id' in request.view_args else None
        )

    def _process_GET(self):
        return jsonify({e.id: FavoriteEventSchema().dump(e) for e in self.user.favorite_events if not e.is_deleted})

    def _process_PUT(self):
        if self.event not in self.user.favorite_events:
            if not self.event.can_access(self.user):
                raise Forbidden
            self.user.favorite_events.add(self.event)
            signals.users.favorite_event_added.send(self.user, event=self.event)
        return jsonify(success=True)

    def _process_DELETE(self):
        if self.event in self.user.favorite_events:
            self.user.favorite_events.discard(self.event)
            try:
                db.session.flush()
            except StaleDataError:
                # Deleted in another transaction
                db.session.rollback()
            signals.users.favorite_event_removed.send(self.user, event=self.event)
        return jsonify(success=True)


class RHUserSuggestionsRemove(RHUserBase):
    def _process(self):
        suggestion = self.user.suggested_categories.filter_by(category_id=request.view_args['category_id']).first()
        if suggestion:
            suggestion.is_ignored = True
        return jsonify(success=True)


class RHUserEmails(RHUserBase):
    def _send_confirmation(self, email):
        token_storage = make_scoped_cache('confirm-email')
        data = {'email': email, 'user_id': self.user.id}
        token = make_unique_token(lambda t: not token_storage.get(t))
        token_storage.set(token, data, timeout=86400)
        with self.user.force_user_locale():
            email_to_send = make_email(email, template=get_template_module('users/emails/verify_email.txt',
                                       user=self.user, email=email, token=token))
            send_email(email_to_send)

    def _process(self):
        form = UserEmailsForm()
        if form.validate_on_submit():
            self._send_confirmation(form.email.data)
            self.user.log(UserLogRealm.user, LogKind.other, 'Profile', 'Validating new secondary email',
                          session.user, data={'Email': form.email.data})
            flash(_('We have sent an email to {email}. Please click the link in that email within 24 hours to '
                    'confirm your new email address.').format(email=form.email.data), 'success')
            return redirect(url_for('.user_emails'))
        return WPUser.render_template('emails.html', 'emails', user=self.user, form=form)


class RHUserEmailsVerify(RHUserBase):
    flash_user_status = False
    token_storage = make_scoped_cache('confirm-email')

    def _validate(self, data):
        if not data:
            flash(_('The verification token is invalid or expired.'), 'error')
            return False, None
        user = User.get(data['user_id'])
        if not user or user != self.user:
            flash(_('This token is for a different Indico user. Please login with the correct account'), 'error')
            return False, None
        existing = UserEmail.query.filter_by(is_user_deleted=False, email=data['email']).first()
        if existing and not existing.user.is_pending:
            if existing.user == self.user:
                flash(_('This email address is already attached to your account.'))
            else:
                flash(_('This email address is already in use by another account.'), 'error')
            return False, existing.user
        return True, existing.user if existing else None

    def _process(self):
        token = request.view_args['token']
        data = self.token_storage.get(token)
        valid, existing = self._validate(data)
        if valid:
            self.token_storage.delete(token)

            if existing and existing.is_pending:
                logger.info('Found pending user %s to be merged into %s', existing, self.user)

                # If the pending user has missing names, copy them from the active one
                # to allow it to be marked as not pending and deleted during the merge.
                existing.first_name = existing.first_name or self.user.first_name
                existing.last_name = existing.last_name or self.user.last_name

                merge_users(existing, self.user)
                flash(_("Merged data from existing '{}' identity").format(existing.email))
                existing.is_pending = False

            self.user.secondary_emails.add(data['email'])
            self.user.log(UserLogRealm.user, LogKind.positive, 'Profile', 'Secondary email added', session.user,
                          data={'Email': data['email']})
            signals.users.email_added.send(self.user, email=data['email'], silent=False)
            flash(_('The email address {email} has been added to your account.').format(email=data['email']), 'success')
        return redirect(url_for('.user_emails'))


class RHUserEmailsDelete(RHUserBase):
    def _process(self):
        email = request.view_args['email']
        if email in self.user.secondary_emails:
            self.user.secondary_emails.remove(email)
            self.user.log(UserLogRealm.user, LogKind.negative, 'Profile', 'Secondary email removed', session.user,
                          data={'Email': email})
        return jsonify(success=True)


class RHUserEmailsSetPrimary(RHUserBase):
    def _process(self):
        from .tasks import update_gravatars

        email = request.form['email']
        if email in self.user.secondary_emails:
            old = self.user.email
            self.user.make_email_primary(email)
            self.user.log(UserLogRealm.user, LogKind.change, 'Profile', 'Primary email updated',
                          session.user, data={'Old': old, 'New': email})
            db.session.commit()
            if self.user.picture_source in (ProfilePictureSource.gravatar, ProfilePictureSource.identicon):
                update_gravatars.delay(self.user)
            flash(_('Your primary email was updated successfully.'), 'success')
            if 'email' in self.user.synced_fields:
                self.user.synced_fields -= {'email'}
                flash(_('Email address synchronization has been disabled since you manually changed your primary'
                        ' email address.'), 'warning')
        return redirect(url_for('.user_emails'))


class RHAdmins(RHAdminBase):
    """Show Indico administrators."""

    def _process(self):
        admins = set(User.query
                     .filter_by(is_admin=True, is_deleted=False)
                     .order_by(db.func.lower(User.first_name), db.func.lower(User.last_name)))

        form = AdminsForm(admins=admins)
        if form.validate_on_submit():
            added = form.admins.data - admins
            removed = admins - form.admins.data
            for user in added:
                user.is_admin = True
                user.log(UserLogRealm.management, LogKind.positive, 'Admins', 'Admin privileges granted', session.user,
                         data={'IP': request.remote_addr})
                logger.warning('Admin rights granted to %r by %r [%s]', user, session.user, request.remote_addr)
                flash(_('Admin added: {name} ({email})').format(name=user.name, email=user.email), 'success')
            for user in removed:
                user.is_admin = False
                user.log(UserLogRealm.management, LogKind.negative, 'Admins', 'Admin privileges revoked', session.user,
                         data={'IP': request.remote_addr})
                logger.warning('Admin rights revoked from %r by %r [%s]', user, session.user, request.remote_addr)
                flash(_('Admin removed: {name} ({email})').format(name=user.name, email=user.email), 'success')
            return redirect(url_for('.admins'))

        return WPUsersAdmin.render_template('admins.html', 'admins', form=form)


class RHUsersAdmin(RHAdminBase):
    """Admin users overview."""

    def _process(self):
        form = SearchForm(obj=FormDefaults(exact=True))
        form_data = form.data
        search_results = None
        num_of_users = User.query.count()
        num_deleted_users = User.query.filter_by(is_deleted=True).count()

        if form.validate_on_submit():
            search_results = []
            exact = form_data.pop('exact')
            include_deleted = form_data.pop('include_deleted')
            include_pending = form_data.pop('include_pending')
            external = form_data.pop('external')
            form_data = {k: v for (k, v) in form_data.items() if v and v.strip()}
            matches = search_users(exact=exact, include_deleted=include_deleted, include_pending=include_pending,
                                   include_blocked=True, external=external, allow_system_user=True, **form_data)
            for entry in matches:
                if isinstance(entry, User):
                    search_results.append(UserEntry(
                        avatar_url=entry.avatar_url,
                        profile_url=url_for('.user_profile', entry),
                        user=entry,
                        **{k: getattr(entry, k) for k in IDENTITY_ATTRIBUTES}
                    ))
                else:
                    if not entry.data['first_name'] and not entry.data['last_name']:
                        full_name = '<no name>'
                        initial = '?'
                    else:
                        full_name = f'{entry.data["first_name"]} {entry.data["last_name"]}'.strip()
                        initial = full_name[0]
                    search_results.append(UserEntry(
                        avatar_url=url_for('assets.avatar', name=initial),
                        profile_url=None,
                        user=None,
                        full_name=full_name,
                        **{k: entry.data.get(k) for k in (IDENTITY_ATTRIBUTES - {'full_name'})}
                    ))
            search_results.sort(key=attrgetter('full_name'))

        num_reg_requests = RegistrationRequest.query.count()
        return WPUsersAdmin.render_template('users_admin.html', 'users', form=form, search_results=search_results,
                                            num_of_users=num_of_users, num_deleted_users=num_deleted_users,
                                            num_reg_requests=num_reg_requests,
                                            has_moderation=multipass.has_moderated_providers)


class RHUsersAdminSettings(RHAdminBase):
    """Manage global user-related settings."""

    def _process(self):
        form = AdminUserSettingsForm(obj=FormDefaults(**user_management_settings.get_all()))
        if form.validate_on_submit():
            user_management_settings.set_multi(form.data)
            return jsonify_data(flash=False)
        return jsonify_form(form)


class RHUsersAdminCreate(RHAdminBase):
    """Create user (admin)."""

    def _process(self):
        form = AdminAccountRegistrationForm()
        if form.validate_on_submit():
            data = form.data
            if data.pop('create_identity', False):
                identifier = data.pop('username') if config.LOCAL_USERNAMES else str(uuid4())
                identity = Identity(provider='indico', identifier=identifier, password=data.pop('password'))
            else:
                identity = None
                data.pop('username', None)
                data.pop('password', None)
            user = create_user(data.pop('email'), data, identity, from_moderation=True)
            msg = Markup('{} <a href="{}">{}</a>').format(
                escape(_('The account has been created.')),
                url_for('users.user_profile', user),
                escape(_('Show details'))
            )
            flash(msg, 'success')
            return jsonify_data()
        return jsonify_template('users/users_admin_create.html', form=form)


def _get_merge_problems(source, target):
    errors = []
    warnings = []
    if source == target:
        errors.append(_('Users are the same!'))
    if (source.first_name.strip().lower() != target.first_name.strip().lower() or
            source.last_name.strip().lower() != target.last_name.strip().lower()):
        warnings.append(_("Users' names seem to be different!"))
    if source.is_pending:
        warnings.append(_('Source user has never logged in to Indico!'))
    if target.is_pending:
        warnings.append(_('Target user has never logged in to Indico!'))
    if source.is_blocked:
        warnings.append(_('Source user is blocked!'))
    if target.is_blocked:
        warnings.append(_('Target user is blocked!'))
    if source.is_deleted:
        errors.append(_('Source user has been deleted!'))
    if target.is_deleted:
        errors.append(_('Target user has been deleted!'))
    if source.is_admin:
        warnings.append(_('Source user is an administrator!'))
    if target.is_admin:
        warnings.append(_('Target user is an administrator!'))
    if source.is_admin and not target.is_admin:
        errors.append(_("Source user is an administrator but target user isn't!"))
    return errors, warnings


class RHUsersAdminMerge(RHAdminBase):
    """Merge users (admin)."""

    def _process(self):
        form = MergeForm()
        if form.validate_on_submit():
            source = form.source_user.data
            target = form.target_user.data
            errors, warnings = _get_merge_problems(source, target)
            if errors:
                raise BadRequest(_('Merge aborted due to failed sanity check'))
            if warnings:
                logger.info('User %s initiated merge of %s into %s (with %d warnings)',
                            session.user, source, target, len(warnings))
            else:
                logger.info('User %s initiated merge of %s into %s', session.user, source, target)
            merge_users(source, target)
            flash(_('The users have been successfully merged.'), 'success')
            return redirect(url_for('.user_profile', user_id=target.id))

        return WPUsersAdmin.render_template('users_merge.html', 'users', form=form)


class RHUsersAdminMergeCheck(RHAdminBase):
    @use_kwargs({
        'source': Principal(allow_external_users=True, required=True),
        'target': Principal(allow_external_users=True, required=True),
    }, location='query')
    def _process(self, source, target):
        errors, warnings = _get_merge_problems(source, target)
        return jsonify(errors=errors, warnings=warnings, source=serialize_user(source), target=serialize_user(target))


class RHRegistrationRequestList(RHAdminBase):
    """List all registration requests."""

    def _process(self):
        requests = RegistrationRequest.query.order_by(RegistrationRequest.email).all()
        return WPUsersAdmin.render_template('registration_requests.html', 'users', pending_requests=requests)


class RHRegistrationRequestBase(RHAdminBase):
    """Base class to process a registration request."""

    def _process_args(self):
        RHAdminBase._process_args(self)
        self.request = RegistrationRequest.get_or_404(request.view_args['request_id'])


class RHAcceptRegistrationRequest(RHRegistrationRequestBase):
    """Accept a registration request."""

    def _process(self):
        if get_user_by_email(self.request.email):
            db.session.delete(self.request)
            flash(_('A user with this email address already exists.'), 'error')
            return jsonify_data()

        user = register_user(self.request.email, self.request.extra_emails, self.request.user_data,
                             self.request.identity_data, self.request.settings)[0]
        with user.force_user_locale():
            tpl = get_template_module('users/emails/registration_request_accepted.txt', user=user)
            email = make_email(self.request.email, template=tpl)
        send_email(email)
        flash(_('The request has been approved.'), 'success')
        return jsonify_data()


class RHRejectRegistrationRequest(RHRegistrationRequestBase):
    """Reject a registration request."""

    def _process(self):
        db.session.delete(self.request)
        with force_locale(None):
            tpl = get_template_module('users/emails/registration_request_rejected.txt', req=self.request)
            email = make_email(self.request.email, template=tpl)
        send_email(email)
        flash(_('The request has been rejected.'), 'success')
        return jsonify_data()


class UserSearchResultSchema(mm.SQLAlchemyAutoSchema):
    affiliation_id = fields.Integer(attribute='affiliation_link.id')
    affiliation_meta = fields.Nested(AffiliationSchema, attribute='affiliation_link')
    title = fields.Enum(UserTitle, attribute='_title')

    class Meta:
        model = User
        fields = ('id', 'identifier', 'email', 'affiliation', 'affiliation_id', 'affiliation_meta',
                  'full_name', 'first_name', 'last_name', 'avatar_url', 'title')


search_result_schema = UserSearchResultSchema()


class RHUserSearchToken(RHProtected):
    """Create a token that allows searching users."""

    @use_kwargs({
        'category': ModelField(Category, filter_deleted=True, load_default=None, data_key='category_id'),
        'event': ModelField(Event, filter_deleted=True, load_default=None, data_key='event_id'),
        'contribution': ModelField(Contribution, filter_deleted=True, load_default=None, data_key='contribution_id'),
        'session': ModelField(Session, filter_deleted=True, load_default=None, data_key='session_id'),
    }, location='query')
    def _process_args(self, category, event, contribution, session):
        self.category = category
        self.event = event
        self.contribution = contribution
        self.session = session

    def _check_access(self):
        RHProtected._check_access(self)
        # XXX for now we do not give admins a token "for free", since this would make spotting bugs
        # where no context is passed much harder. of course any of the access checks below will still
        # be short-circuited for an admin, so calling this endpoint with `category_id=0` would always
        # work for an admin
        if self.category and self.category.can_create_events(session.user):
            return
        elif self.event and self.event.can_manage(session.user):
            return
        elif self.contribution and self.contribution.can_manage(session.user):
            return
        elif self.session and self.session.can_manage(session.user):
            return
        else:
            raise Forbidden('Not authorized to search users')

    def _process(self):
        return jsonify(token=make_user_search_token())


class RHUserSearch(RHProtected):
    """Search for users based on given criteria."""

    @use_kwargs({
        'token': fields.String(load_default=''),
    }, location='query')
    def _check_access(self, token):
        RHProtected._check_access(self)
        validate_search_token(token, session.user)

    def _serialize_pending_user(self, entry):
        first_name = entry.data.get('first_name') or ''
        last_name = entry.data.get('last_name') or ''
        full_name = f'{first_name} {last_name}'.strip() or 'Unknown'
        affiliation = entry.data.get('affiliation') or ''
        affiliation_data = entry.data.get('affiliation_data')
        email = entry.data['email'].lower()
        ext_id = f'{entry.provider.name}:{entry.identifier}'
        # IdentityInfo from flask-multipass does not have `avatar_url`
        avatar_url = get_avatar_url_from_name(first_name)

        # detailed data to put in redis to create a pending user if needed
        self.externals[ext_id] = {
            'first_name': first_name,
            'last_name': last_name,
            'email': email,
            'affiliation': affiliation,
            'affiliation_data': affiliation_data,
            'phone': entry.data.get('phone') or '',
            'address': entry.data.get('address') or '',
        }
        # simple data for the search results
        return {
            '_ext_id': ext_id,
            'id': None,
            'identifier': f'ExternalUser:{ext_id}',
            'email': email,
            'affiliation': affiliation,
            'affiliation_id': -1 if affiliation_data else None,
            'affiliation_meta': (AffiliationSchema().dump(affiliation_data) | {'id': -1}) if affiliation_data else None,
            'full_name': full_name,
            'first_name': first_name,
            'last_name': last_name,
            'avatar_url': avatar_url
        }

    def _serialize_entry(self, entry):
        if isinstance(entry, User):
            return search_result_schema.dump(entry)
        else:
            return self._serialize_pending_user(entry)

    def _process_pending_users(self, results):
        cache = make_scoped_cache('external-user')
        for entry in results:
            ext_id = entry.pop('_ext_id', None)
            if ext_id is not None:
                cache.set(ext_id, self.externals[ext_id], timeout=86400)

    @use_kwargs({
        'first_name': fields.Str(validate=validate.Length(min=1)),
        'last_name': fields.Str(validate=validate.Length(min=1)),
        'email': fields.Str(validate=lambda s: len(s) > 3),
        'affiliation': fields.Str(validate=validate.Length(min=1)),
        'exact': fields.Bool(load_default=False),
        'external': fields.Bool(load_default=False),
        'favorites_first': fields.Bool(load_default=False)
    }, validate=validate_with_message(
        lambda args: args.keys() & {'first_name', 'last_name', 'email', 'affiliation'},
        'No criteria provided'
    ), location='query')
    def _process(self, exact, external, favorites_first, **criteria):
        matches = search_users(exact=exact, include_pending=True, external=external, **criteria)
        self.externals = {}

        def _sort_key(entry):
            # Sort results by providing exact matches first, initially considering accents, and
            # then without considering accents.
            exact_match_keys = [entry[k].lower() != v.lower() for k, v in criteria.items()]
            unaccent_exact_match_keys = [
                remove_accents(entry[k].lower()) != remove_accents(v.lower())
                for k, v in criteria.items()
            ]
            return *exact_match_keys, *unaccent_exact_match_keys, entry['full_name'], entry['email']

        results = sorted((self._serialize_entry(entry) for entry in matches), key=_sort_key)
        if favorites_first:
            favorites = {u.id for u in session.user.favorite_users}
            results.sort(key=lambda x: x['id'] not in favorites)
        total = len(results)
        results = results[:10]
        self._process_pending_users(results)
        return jsonify(users=results, total=total)


class RHUserSearchInfo(RHProtected):
    def _process(self):
        external_users_available = any(auth.supports_search for auth in multipass.identity_providers.values())
        return jsonify(external_users_available=external_users_available)


class RHUserBlock(RHUserBase):
    def _check_access(self):
        RHUserBase._check_access(self)
        if not session.user.is_admin:
            raise Forbidden

    def _process_PUT(self):
        if self.user == session.user:
            raise Forbidden(_('You cannot block yourself'))
        self.user.is_blocked = True
        self.user.log(UserLogRealm.management, LogKind.negative, 'User', 'User blocked', session.user)
        logger.info('User %s blocked %s', session.user, self.user)
        flash(_('{name} has been blocked.').format(name=self.user.name), 'success')
        return jsonify(success=True)

    def _process_DELETE(self):
        self.user.is_blocked = False
        self.user.log(UserLogRealm.management, LogKind.positive, 'User', 'User unblocked', session.user)
        logger.info('User %s unblocked %s', session.user, self.user)
        flash(_('{name} has been unblocked.').format(name=self.user.name), 'success')
        return jsonify(success=True)


class RHUserDelete(RHUserBase):
    """Delete or anonymize a user.

    Deletes the user, and all their associated data. If it is not possible to delete the user, it will
    instead fallback to anonymizing the user.
    """

    def _check_access(self):
        RHUserBase._check_access(self)
        if not session.user.is_admin or not config.ALLOW_ADMIN_USER_DELETION:
            raise Forbidden
        if self.user == session.user:
            raise Forbidden('You cannot delete your own account.')
        if self.user.is_admin:
            raise Forbidden('You cannot delete an admin account.')

    def _process(self):
        user_name = self.user.name
        delete_or_anonymize_user(self.user)
        if self.user not in db.session:
            flash(_('{user_name} has been deleted.').format(user_name=user_name), 'success')
        else:
            flash(_('{user_name} has been anonymized.').format(user_name=user_name), 'success')
        return '', 204
