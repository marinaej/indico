# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import itertools
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest
from flask import session

from indico.core.db import db
from indico.core.errors import UserValueError
from indico.modules.events.models.persons import EventPerson
from indico.modules.events.registration.controllers.management.fields import _fill_form_field_with_data
from indico.modules.events.registration.models.form_fields import RegistrationFormField
from indico.modules.events.registration.models.invitations import RegistrationInvitation
from indico.modules.events.registration.models.items import RegistrationFormItemType, RegistrationFormSection
from indico.modules.events.registration.models.registrations import RegistrationVisibility
from indico.modules.events.registration.util import (create_registration, get_event_regforms_registrations,
                                                     get_registered_event_persons, get_ticket_qr_code_data,
                                                     get_user_data, import_invitations_from_csv,
                                                     import_registrations_from_csv, import_user_records_from_csv,
                                                     modify_registration)
from indico.modules.users.models.users import UserTitle
from indico.testing.util import assert_json_snapshot


pytest_plugins = 'indico.modules.events.registration.testing.fixtures'


@pytest.mark.usefixtures('dummy_regform')
def test_import_users():
    csv = b'\n'.join([b'John,Doe,ACME Inc.,Regional Manager,+1-202-555-0140,jdoe@example.test',
                      b'Jane,Smith,ACME Inc.,CEO,,jane@example.test',
                      b'Billy Bob,Doe,,,,1337@EXAMPLE.test'])

    columns = ['first_name', 'last_name', 'affiliation', 'position', 'phone', 'email']
    users = import_user_records_from_csv(BytesIO(csv), columns)
    assert len(users) == 3

    assert users[0] == {
        'first_name': 'John',
        'last_name': 'Doe',
        'affiliation': 'ACME Inc.',
        'position': 'Regional Manager',
        'phone': '+1-202-555-0140',
        'email': 'jdoe@example.test',
    }

    assert users[1] == {
        'first_name': 'Jane',
        'last_name': 'Smith',
        'affiliation': 'ACME Inc.',
        'position': 'CEO',
        'phone': '',
        'email': 'jane@example.test',
    }

    assert users[2] == {
        'first_name': 'Billy Bob',
        'last_name': 'Doe',
        'affiliation': '',
        'position': '',
        'phone': '',
        'email': '1337@example.test',
    }


def test_import_users_error(create_user):
    columns = ['first_name', 'last_name', 'affiliation', 'position', 'phone', 'email']
    user = create_user(123, email='test1@example.test')
    user.secondary_emails.add('test2@example.test')

    # missing column
    csv = b'\n'.join([b'John,Doe,ACME Inc.,Regional Manager,+1-202-555-0140,jdoe@example.test',
                      b'Buggy,Entry,ACME Inc.,CEO,'])

    with pytest.raises(UserValueError) as e:
        import_user_records_from_csv(BytesIO(csv), columns)
    assert 'malformed' in str(e.value)
    assert 'Row 2' in str(e.value)

    # missing e-mail
    csv = b'\n'.join([b'Bill,Doe,ACME Inc.,Regional Manager,+1-202-555-0140,bdoe@example.test',
                      b'Buggy,Entry,ACME Inc.,CEO,,'])

    with pytest.raises(UserValueError) as e:
        import_user_records_from_csv(BytesIO(csv), columns)
    assert 'missing e-mail' in str(e.value)
    assert 'Row 2' in str(e.value)

    # bad e-mail
    csv = b'\n'.join([b'Bill,Doe,ACME Inc.,Regional Manager,+1-202-555-0140,bdoe@example.test',
                      b'Buggy,Entry,ACME Inc.,CEO,,not-an-email'])

    with pytest.raises(UserValueError) as e:
        import_user_records_from_csv(BytesIO(csv), columns)
    assert 'invalid e-mail' in str(e.value)
    assert 'Row 2' in str(e.value)

    # duplicate e-mail
    csv = b'\n'.join([b'Bill,Doe,ACME Inc.,Regional Manager,+1-202-555-0140,bdoe@example.test',
                      b'Bob,Doe,ACME Inc.,Boss,,bdoe@example.test'])

    with pytest.raises(UserValueError) as e:
        import_user_records_from_csv(BytesIO(csv), columns)
    assert 'email address is not unique' in str(e.value)
    assert 'Row 2' in str(e.value)

    # duplicate user
    csv = b'\n'.join([b'Big,Boss,ACME Inc.,Supreme Leader,+1-202-555-1337,test1@example.test',
                      b'Little,Boss,ACME Inc.,Wannabe Leader,+1-202-555-1338,test2@EXAMPLE.test'])

    with pytest.raises(UserValueError) as e:
        import_user_records_from_csv(BytesIO(csv), columns)
    assert 'Row 2: email address belongs to the same user as in row 1' in str(e.value)

    # missing first name
    csv = b'\n'.join([b'Ray,Doe,ACME Inc.,Regional Manager,+1-202-555-0140,rdoe@example.test',
                      b',Buggy,ACME Inc.,CEO,,buggy@example.test'])

    with pytest.raises(UserValueError) as e:
        import_user_records_from_csv(BytesIO(csv), columns)
    assert 'missing first' in str(e.value)
    assert 'Row 2' in str(e.value)


def test_import_registrations(dummy_regform, dummy_user):
    csv = b'\n'.join([b'John,Doe,ACME Inc.,Regional Manager,+1-202-555-0140,jdoe@example.test',
                      b'Jane,Smith,ACME Inc.,CEO,,jane@example.test',
                      b'Billy Bob,Doe,,,,1337@EXAMPLE.test'])
    registrations = import_registrations_from_csv(dummy_regform, BytesIO(csv))
    assert len(registrations) == 3

    assert registrations[0].full_name == 'John Doe'
    assert registrations[0].user is None
    data = registrations[0].get_personal_data()
    assert data['affiliation'] == 'ACME Inc.'
    assert data['email'] == 'jdoe@example.test'
    assert data['position'] == 'Regional Manager'
    assert data['phone'] == '+1-202-555-0140'

    assert registrations[1].full_name == 'Jane Smith'
    assert registrations[1].user is None
    data = registrations[1].get_personal_data()
    assert data['affiliation'] == 'ACME Inc.'
    assert data['email'] == 'jane@example.test'
    assert data['position'] == 'CEO'
    assert 'phone' not in data

    assert registrations[2].full_name == 'Billy Bob Doe'
    assert registrations[2].user == dummy_user
    data = registrations[2].get_personal_data()
    assert 'affiliation' not in data
    assert data['email'] == '1337@example.test'
    assert 'position' not in data
    assert 'phone' not in data


def test_import_registrations_error(dummy_regform, dummy_user):
    dummy_user.secondary_emails.add('dummy@example.test')

    create_registration(dummy_regform, {
        'email': dummy_user.email,
        'first_name': dummy_user.first_name,
        'last_name': dummy_user.last_name
    }, notify_user=False)

    create_registration(dummy_regform, {
        'email': 'boss@example.test',
        'first_name': 'Big',
        'last_name': 'Boss'
    }, notify_user=False)

    # duplicate e-mail
    csv = b'\n'.join([b'Big,Boss,ACME Inc.,Supreme Leader,+1-202-555-1337,boss@example.test'])

    with pytest.raises(UserValueError) as e:
        import_registrations_from_csv(dummy_regform, BytesIO(csv))
    assert 'a registration with this email already exists' in str(e.value)
    assert 'Row 1' in str(e.value)

    # duplicate user
    csv = b'\n'.join([b'Big,Boss,ACME Inc.,Supreme Leader,+1-202-555-1337,dummy@example.test'])

    with pytest.raises(UserValueError) as e:
        import_registrations_from_csv(dummy_regform, BytesIO(csv))
    assert 'a registration for this user already exists' in str(e.value)
    assert 'Row 1' in str(e.value)


def test_import_invitations(monkeypatch, dummy_regform, dummy_user):
    monkeypatch.setattr('indico.modules.events.registration.util.notify_invitation', lambda *args, **kwargs: None)

    # normal import with no conflicts
    csv = b'\n'.join([b'Bob,Doe,ACME Inc.,bdoe@example.test',
                      b'Jane,Smith,ACME Inc.,jsmith@example.test'])
    invitations, skipped = import_invitations_from_csv(dummy_regform, BytesIO(csv),
                                                       email_sender='noreply@example.test', email_subject='invitation',
                                                       email_body='Invitation to event',
                                                       skip_moderation=False, skip_access_check=False,
                                                       skip_existing=True)
    assert len(invitations) == 2
    assert skipped == 0

    assert invitations[0].first_name == 'Bob'
    assert invitations[0].last_name == 'Doe'
    assert invitations[0].affiliation == 'ACME Inc.'
    assert invitations[0].email == 'bdoe@example.test'
    assert not invitations[0].skip_moderation
    assert not invitations[0].skip_access_check

    assert invitations[1].first_name == 'Jane'
    assert invitations[1].last_name == 'Smith'
    assert invitations[1].affiliation == 'ACME Inc.'
    assert invitations[1].email == 'jsmith@example.test'
    assert not invitations[1].skip_moderation
    assert not invitations[1].skip_access_check


def test_import_invitations_duplicate_invitation(monkeypatch, dummy_regform, dummy_user):
    monkeypatch.setattr('indico.modules.events.registration.util.notify_invitation', lambda *args, **kwargs: None)

    invitation = RegistrationInvitation(skip_moderation=True, email='awang@example.test', first_name='Amy',
                                        last_name='Wang', affiliation='ACME Inc.')
    dummy_regform.invitations.append(invitation)

    # duplicate invitation with 'skip_existing=True'
    csv = b'\n'.join([b'Amy,Wang,ACME Inc.,awang@example.test',
                      b'Jane,Smith,ACME Inc.,jsmith@example.test'])
    invitations, skipped = import_invitations_from_csv(dummy_regform, BytesIO(csv),
                                                       email_sender='noreply@example.test', email_subject='invitation',
                                                       email_body='Invitation to event',
                                                       skip_moderation=True, skip_existing=True)
    assert len(invitations) == 1
    assert skipped == 1

    assert invitations[0].first_name == 'Jane'
    assert invitations[0].last_name == 'Smith'
    assert invitations[0].affiliation == 'ACME Inc.'
    assert invitations[0].email == 'jsmith@example.test'
    assert invitations[0].skip_moderation
    assert invitations[0].skip_access_check


def test_import_invitations_duplicate_registration(monkeypatch, dummy_regform):
    monkeypatch.setattr('indico.modules.events.registration.util.notify_invitation', lambda *args, **kwargs: None)

    create_registration(dummy_regform, {
        'email': 'boss@example.test',
        'first_name': 'Big',
        'last_name': 'Boss'
    }, notify_user=False)

    # duplicate registration with 'skip_existing=True'
    csv = b'\n'.join([b'Big,Boss,ACME Inc.,boss@example.test',
                      b'Jane,Smith,ACME Inc.,jsmith@example.test'])
    invitations, skipped = import_invitations_from_csv(dummy_regform, BytesIO(csv),
                                                       email_sender='noreply@example.test', email_subject='invitation',
                                                       email_body='Invitation to event',
                                                       skip_moderation=True, skip_existing=True)
    assert len(invitations) == 1
    assert skipped == 1

    assert invitations[0].first_name == 'Jane'
    assert invitations[0].last_name == 'Smith'
    assert invitations[0].affiliation == 'ACME Inc.'
    assert invitations[0].email == 'jsmith@example.test'
    assert invitations[0].skip_moderation
    assert invitations[0].skip_access_check


def test_import_invitations_duplicate_user(monkeypatch, dummy_regform, dummy_user):
    monkeypatch.setattr('indico.modules.events.registration.util.notify_invitation', lambda *args, **kwargs: None)

    dummy_user.secondary_emails.add('dummy@example.test')
    create_registration(dummy_regform, {
        'email': dummy_user.email,
        'first_name': dummy_user.first_name,
        'last_name': dummy_user.last_name
    }, notify_user=False)

    # duplicate user with 'skip_existing=True'
    csv = b'\n'.join([b'Big,Boss,ACME Inc.,dummy@example.test',
                      b'Jane,Smith,ACME Inc.,jsmith@example.test'])
    invitations, skipped = import_invitations_from_csv(dummy_regform, BytesIO(csv),
                                                       email_sender='noreply@example.test', email_subject='invitation',
                                                       email_body='Invitation to event',
                                                       skip_moderation=True, skip_existing=True)
    assert len(invitations) == 1
    assert skipped == 1

    assert invitations[0].first_name == 'Jane'
    assert invitations[0].last_name == 'Smith'
    assert invitations[0].affiliation == 'ACME Inc.'
    assert invitations[0].email == 'jsmith@example.test'
    assert invitations[0].skip_moderation
    assert invitations[0].skip_access_check


def test_import_invitations_error(dummy_regform, dummy_user):
    dummy_user.secondary_emails.add('dummy@example.test')

    create_registration(dummy_regform, {
        'email': dummy_user.email,
        'first_name': dummy_user.first_name,
        'last_name': dummy_user.last_name
    }, notify_user=False)

    create_registration(dummy_regform, {
        'email': 'boss@example.test',
        'first_name': 'Big',
        'last_name': 'Boss'
    }, notify_user=False)

    invitation = RegistrationInvitation(skip_moderation=True, email='bdoe@example.test', first_name='Bill',
                                        last_name='Doe', affiliation='ACME Inc.')
    dummy_regform.invitations.append(invitation)

    # duplicate e-mail (registration)
    csv = b'\n'.join([b'Big,Boss,ACME Inc.,boss@example.test'])

    with pytest.raises(UserValueError) as e:
        import_invitations_from_csv(dummy_regform, BytesIO(csv),
                                    email_sender='noreply@example.test', email_subject='invitation',
                                    email_body='Invitation to event',
                                    skip_moderation=False, skip_existing=False)
    assert 'a registration with this email already exists' in str(e.value)
    assert 'Row 1' in str(e.value)

    # duplicate user
    csv = b'\n'.join([b'Big,Boss,ACME Inc.,dummy@example.test'])

    with pytest.raises(UserValueError) as e:
        import_invitations_from_csv(dummy_regform, BytesIO(csv),
                                    email_sender='noreply@example.test', email_subject='invitation',
                                    email_body='Invitation to event',
                                    skip_moderation=False, skip_existing=False)
    assert 'a registration for this user already exists' in str(e.value)
    assert 'Row 1' in str(e.value)

    # duplicate email (invitation)
    csv = b'\n'.join([b'Bill,Doe,ACME Inc.,bdoe@example.test'])
    with pytest.raises(UserValueError) as e:
        import_invitations_from_csv(dummy_regform, BytesIO(csv),
                                    email_sender='noreply@example.test', email_subject='invitation',
                                    email_body='Invitation to event',
                                    skip_moderation=False, skip_existing=False)
    assert 'an invitation for this user already exists' in str(e.value)
    assert 'Row 1' in str(e.value)


@pytest.mark.parametrize(('start_dt', 'end_dt', 'include_scheduled', 'expected_regform_flag'), (
    (datetime(2007, 1, 1, 1, 0, 0), datetime(2007, 2, 1, 1, 0, 0), False, False),
    (datetime(2019, 1, 1, 1, 0, 0), datetime(2020, 2, 1, 1, 0, 0), False, True),
    (datetime(2007, 1, 1, 1, 0, 0), datetime(2007, 2, 1, 1, 0, 0), True, True),
    (datetime(2019, 1, 1, 1, 0, 0), datetime(2020, 2, 1, 1, 0, 0), True, True),
    (None, datetime(2020, 2, 1, 1, 0, 0), False, False),
    (None, datetime(2020, 2, 1, 1, 0, 0), True, False),
    (datetime(2019, 1, 1, 1, 0, 0), None, False, True),
    (None, None, False, False),
    (None, None, True, False)
))
def test_get_event_regforms_no_registration(dummy_event, dummy_user, dummy_regform, freeze_time, start_dt, end_dt,
                                            include_scheduled, expected_regform_flag):
    freeze_time(datetime(2019, 12, 13, 8, 0, 0))
    if start_dt:
        dummy_regform.start_dt = dummy_event.tzinfo.localize(start_dt)
    if end_dt:
        dummy_regform.end_dt = dummy_event.tzinfo.localize(end_dt)

    regforms, registrations = get_event_regforms_registrations(dummy_event, dummy_user, include_scheduled)

    assert (dummy_regform in regforms) == expected_regform_flag
    assert list(registrations.values()) == [None]


@pytest.mark.parametrize(('start_dt', 'end_dt', 'include_scheduled'), (
    (datetime(2019, 1, 1, 1, 0, 0), datetime(2019, 2, 1, 1, 0, 0), True),
    (datetime(2018, 1, 1, 1, 0, 0), datetime(2018, 12, 1, 1, 0, 0), False),
    (datetime(2019, 1, 1, 1, 0, 0), datetime(2020, 2, 1, 1, 0, 0), False),
    (None, None, False),
    (datetime(2008, 1, 1, 1, 0, 0), None, False),
    (None, datetime(2020, 12, 1, 1, 0, 0), True),
))
@pytest.mark.usefixtures('dummy_reg')
def test_get_event_regforms_registration(dummy_event, dummy_user, dummy_regform, start_dt, end_dt, include_scheduled,
                                         freeze_time):
    freeze_time(datetime(2019, 12, 13, 8, 0, 0))
    if start_dt:
        dummy_regform.start_dt = dummy_event.tzinfo.localize(start_dt)
    if end_dt:
        dummy_regform.end_dt = dummy_event.tzinfo.localize(end_dt)

    regforms, registrations = get_event_regforms_registrations(dummy_event, dummy_user, include_scheduled=False)

    assert list(registrations.values())[0].user == dummy_user
    assert dummy_regform in regforms


@pytest.mark.usefixtures('dummy_reg')
def test_get_registered_event_persons(dummy_event, dummy_user, dummy_regform):
    create_registration(dummy_regform, {
        'email': 'john@example.test',
        'first_name': 'John',
        'last_name': 'Doe',
    }, notify_user=False)

    user_person = EventPerson.create_from_user(dummy_user, dummy_event)
    no_user_person = EventPerson(
        email='john@example.test',
        first_name='John',
        last_name='Doe'
    )

    create_registration(dummy_regform, {
        'email': 'jane@example.test',
        'first_name': 'Jane',
        'last_name': 'Doe',
    }, notify_user=False)

    no_user_no_reg = EventPerson(
        email='noshow@example.test',
        first_name='No',
        last_name='Show'
    )
    dummy_event.persons.append(user_person)
    dummy_event.persons.append(no_user_person)
    dummy_event.persons.append(no_user_no_reg)
    db.session.flush()

    registered_persons = get_registered_event_persons(dummy_event)
    assert registered_persons == {user_person, no_user_person}


def test_create_registration(monkeypatch, dummy_user, dummy_regform):
    monkeypatch.setattr('indico.modules.users.util.get_user_by_email', lambda *args, **kwargs: dummy_user)

    # Extend the dummy_regform with more sections and fields
    section = RegistrationFormSection(registration_form=dummy_regform, title='dummy_section', is_manager_only=False)

    boolean_field = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(boolean_field, {
        'input_type': 'bool', 'default_value': False, 'title': 'Yes/No'
    })

    multi_choice_field = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(multi_choice_field, {
        'input_type': 'multi_choice', 'with_extra_slots': False, 'title': 'Multi Choice',
        'choices': [
            {'caption': 'A', 'id': 'new:test1', 'is_enabled': True},
            {'caption': 'B', 'id': 'new:test2', 'is_enabled': True},
        ]
    })
    db.session.flush()

    data = {
        boolean_field.html_field_name: True,
        multi_choice_field.html_field_name: {'test1': 2},
        'email': dummy_user.email, 'first_name': dummy_user.first_name, 'last_name': dummy_user.last_name
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {'test1': 2}
    db.session.delete(reg)
    db.session.flush()

    # Make sure that missing data gets default values:
    data = {
        'email': dummy_user.email, 'first_name': dummy_user.first_name, 'last_name': dummy_user.last_name
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert not reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {}
    db.session.delete(reg)
    db.session.flush()

    # Add a manager only section
    section = RegistrationFormSection(registration_form=dummy_regform, title='manager_section', is_manager_only=True)

    checkbox_field = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(checkbox_field, {
        'input_type': 'checkbox', 'title': 'Checkbox'
    })
    db.session.flush()

    data = {
        checkbox_field.html_field_name: True,
        'email': dummy_user.email, 'first_name': dummy_user.first_name, 'last_name': dummy_user.last_name
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert not reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {}
    # Assert that the manager field gets the default value, not the value sent
    assert not reg.data_by_field[checkbox_field.id].data
    db.session.delete(reg)
    db.session.flush()

    # Try again with management=True
    data = {
        checkbox_field.html_field_name: True,
        'email': dummy_user.email, 'first_name': dummy_user.first_name, 'last_name': dummy_user.last_name
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=True, notify_user=False)

    assert not reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {}
    # Assert that the manager field gets properly set with management=True
    assert reg.data_by_field[checkbox_field.id].data


@pytest.mark.usefixtures('request_context')
def test_modify_registration(monkeypatch, dummy_user, dummy_regform):
    monkeypatch.setattr('indico.modules.users.util.get_user_by_email', lambda *args, **kwargs: dummy_user)

    # Extend the dummy_regform with more sections and fields
    user_section = RegistrationFormSection(registration_form=dummy_regform,
                                           title='dummy_section', is_manager_only=False)

    boolean_field = RegistrationFormField(parent=user_section, registration_form=dummy_regform)
    _fill_form_field_with_data(boolean_field, {
        'input_type': 'bool', 'default_value': False, 'title': 'Yes/No'
    })

    multi_choice_field = RegistrationFormField(parent=user_section, registration_form=dummy_regform)
    _fill_form_field_with_data(multi_choice_field, {
        'input_type': 'multi_choice', 'with_extra_slots': False, 'title': 'Multi Choice',
        'choices': [
            {'caption': 'A', 'id': 'new:test1', 'is_enabled': True},
            {'caption': 'B', 'id': 'new:test2', 'is_enabled': True},
        ]
    })
    choice_uuid = next(k for k, v in multi_choice_field.data['captions'].items() if v == 'A')

    # Add a manager-only section
    management_section = RegistrationFormSection(registration_form=dummy_regform,
                                                 title='manager_section', is_manager_only=True)

    checkbox_field = RegistrationFormField(parent=management_section, registration_form=dummy_regform)
    _fill_form_field_with_data(checkbox_field, {
        'input_type': 'checkbox', 'is_required': True, 'title': 'Checkbox'
    })
    db.session.flush()

    # Create a registration
    data = {
        boolean_field.html_field_name: True,
        multi_choice_field.html_field_name: {choice_uuid: 2},
        checkbox_field.html_field_name: True,
        'email': dummy_user.email, 'first_name': dummy_user.first_name, 'last_name': dummy_user.last_name
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=True, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {choice_uuid: 2}
    assert reg.data_by_field[checkbox_field.id].data

    # Modify the registration without re-sending unchanged data
    data = {
        multi_choice_field.html_field_name: {choice_uuid: 1},
        checkbox_field.html_field_name: False,  # manager-only --> value must be ignored
    }
    modify_registration(reg, data, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {choice_uuid: 1}
    # Assert that the manager field is not changed
    assert reg.data_by_field[checkbox_field.id].data

    # Modify the registration
    data = {
        boolean_field.html_field_name: True,  # unmodified, but re-sending it is allowed
        multi_choice_field.html_field_name: {choice_uuid: 1},
        checkbox_field.html_field_name: False,  # manager-only --> value must be ignored
    }
    modify_registration(reg, data, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {choice_uuid: 1}
    # Assert that the manager field is not changed
    assert reg.data_by_field[checkbox_field.id].data

    # Modify as a manager
    data = {
        multi_choice_field.html_field_name: {choice_uuid: 3},
        checkbox_field.html_field_name: False,
    }
    modify_registration(reg, data, management=True, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {choice_uuid: 3}
    assert not reg.data_by_field[checkbox_field.id].data

    # Add a new field after registering
    new_multi_choice_field = RegistrationFormField(parent=user_section, registration_form=dummy_regform)
    _fill_form_field_with_data(new_multi_choice_field, {
        'input_type': 'multi_choice', 'with_extra_slots': False, 'title': 'Multi Choice',
        'choices': [
            {'caption': 'A', 'id': 'new:test3', 'is_enabled': True},
        ]
    })
    db.session.flush()

    modify_registration(reg, {}, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[multi_choice_field.id].data == {choice_uuid: 3}
    assert not reg.data_by_field[checkbox_field.id].data
    # Assert that the new field got a default value
    assert reg.data_by_field[new_multi_choice_field.id].data == {}

    # Remove a field after registering
    multi_choice_field.is_deleted = True
    db.session.flush()

    data = {
        multi_choice_field.html_field_name: {choice_uuid: 7},
    }
    modify_registration(reg, data, management=True, notify_user=False)
    assert reg.data_by_field[boolean_field.id].data
    # Assert that the removed field keeps its old value
    assert reg.data_by_field[multi_choice_field.id].data == {choice_uuid: 3}
    assert not reg.data_by_field[checkbox_field.id].data
    assert reg.data_by_field[new_multi_choice_field.id].data == {}


@pytest.mark.usefixtures('request_context')
def test_modify_registration_update_consent(dummy_reg):
    # Ensure that consent_to_publish is updated appropriately
    changes = {'consent_to_publish': RegistrationVisibility.all}
    modify_registration(dummy_reg, changes, management=False, notify_user=False)
    assert dummy_reg.consent_to_publish == RegistrationVisibility.all

    # 'consent_to_publish' is not in the changes and thus should not be modified
    changes = {}
    # Set consent_to_publish to a non-default value
    dummy_reg.consent_to_publish = RegistrationVisibility.all
    modify_registration(dummy_reg, changes, management=False, notify_user=False)
    assert dummy_reg.consent_to_publish == RegistrationVisibility.all


@pytest.mark.usefixtures('request_context')
def test_get_user_data(monkeypatch, dummy_event, dummy_user, dummy_regform):
    monkeypatch.setattr('indico.modules.events.registration.util.notify_invitation', lambda *args, **kwargs: None)
    session.set_session_user(dummy_user)

    assert get_user_data(dummy_regform, None) == {}

    expected = {'email': '1337@example.test', 'first_name': 'Guinea',
                'last_name': 'Pig'}

    user_data = get_user_data(dummy_regform, dummy_user)
    assert user_data == expected

    user_data = get_user_data(dummy_regform, session.user)
    assert user_data == expected

    dummy_user.title = UserTitle.mr
    dummy_user.phone = '+1 22 50 14'
    dummy_user.address = 'Geneva'
    user_data = get_user_data(dummy_regform, dummy_user)
    assert type(user_data['title']) is dict
    assert user_data['phone'] == '+1 22 50 14'

    # Check that data is taken from the invitation
    invitation = RegistrationInvitation(skip_moderation=True, email='awang@example.test', first_name='Amy',
                                        last_name='Wang', affiliation='ACME Inc.')
    dummy_regform.invitations.append(invitation)

    dummy_user.title = None
    user_data = get_user_data(dummy_regform, dummy_user, invitation)
    assert user_data == {'email': 'awang@example.test', 'first_name': 'Amy', 'last_name': 'Wang',
                         'phone': '+1 22 50 14', 'address': 'Geneva', 'affiliation': 'ACME Inc.'}

    # Check that data is taken from the invitation when user is missing
    user_data = get_user_data(dummy_regform, None, invitation)
    assert user_data == {'email': 'awang@example.test', 'first_name': 'Amy', 'last_name': 'Wang',
                         'affiliation': 'ACME Inc.'}

    # Check that data from disabled/deleted fields is not used
    title_field = next(item for item in dummy_regform.active_fields
                       if item.type == RegistrationFormItemType.field_pd and item.personal_data_type.name == 'title')
    title_field.is_enabled = False

    dummy_user.title = UserTitle.dr
    user_data = get_user_data(dummy_regform, dummy_user)
    assert 'title' not in user_data

    phone_field = next(item for item in dummy_regform.active_fields
                       if item.type == RegistrationFormItemType.field_pd and item.personal_data_type.name == 'phone')
    phone_field.is_deleted = True

    user_data = get_user_data(dummy_regform, dummy_user)
    assert 'title' not in user_data
    assert 'phone' not in user_data
    assert user_data == {'email': '1337@example.test', 'first_name': 'Guinea',
                         'last_name': 'Pig', 'address': 'Geneva'}

    for item in dummy_regform.active_fields:
        item.is_enabled = False

    assert get_user_data(dummy_regform, dummy_user) == {}
    assert get_user_data(dummy_regform, dummy_user, invitation) == {}


@pytest.mark.parametrize(('url', 'ticket_uuid', 'person_id'), (
    ('https://indico.cern.ch', '9982be4e-32cf-4656-a781-62ad45609d12', None),
    ('http://indico.cern.ch', '9982be4e-32cf-4656-a781-62ad45609d12', None),
    ('https://indico.cern.ch', '9982be4e-32cf-4656-a781-62ad45609d12', '5fa6d71b-a828-4811-bf9c-7e99df04d0af'),
), ids=itertools.count())
def test_get_ticket_qr_code_data(request, mocker, snapshot, dummy_reg, url, ticket_uuid, person_id):
    class MockConfig:
        BASE_URL = url

    mocker.patch('indico.modules.events.registration.util.config', MockConfig())

    dummy_reg.ticket_uuid = ticket_uuid
    person = {
        'registration': dummy_reg,
        'id': person_id,
        'is_accompanying': bool(person_id),
    }

    data = get_ticket_qr_code_data(person)
    snapshot.snapshot_dir = Path(__file__).parent / 'tests'
    assert_json_snapshot(snapshot, data, f'ticket_qr_code_data-{request.node.callspec.id}.json')


def test_create_registration_conditional(monkeypatch, dummy_user, dummy_regform):
    monkeypatch.setattr('indico.modules.users.util.get_user_by_email', lambda *args, **kwargs: dummy_user)

    # Extend the dummy_regform with more sections and fields
    section = RegistrationFormSection(registration_form=dummy_regform, title='dummy_section', is_manager_only=False)

    boolean_field = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(boolean_field, {
        'input_type': 'bool', 'default_value': False, 'title': 'Bool 1'
    })
    db.session.flush()

    boolean_field_2 = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(boolean_field_2, {
        'input_type': 'bool', 'title': 'Bool 2',
        'show_if_field_id': boolean_field.id,
        'show_if_field_values': [True],
    })
    db.session.flush()

    text_field = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(text_field, {
        'input_type': 'text', 'title': 'Cond Text',
        'show_if_field_id': boolean_field_2.id,
        'show_if_field_values': [False],
    })
    db.session.flush()

    personal_data = {'email': dummy_user.email, 'first_name': dummy_user.first_name, 'last_name': dummy_user.last_name}

    # Register with the conditional field disabled, but data present. This is ignored here because it's only
    # actively rejected on the schema level, while being silently ignored in create_registration
    data = {
        **personal_data,
        boolean_field.html_field_name: False,
        text_field.html_field_name: 'meow',
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert not reg.data_by_field[boolean_field.id].data
    assert text_field.id not in reg.data_by_field  # disabled conditional field cannot have data
    db.session.delete(reg)
    db.session.flush()

    # Register with the conditional field disabled, but data present. This is ignored here because it's only
    # actively rejected on the schema level, while being silently ignored in create_registration
    data = {
        **personal_data,
        boolean_field.html_field_name: True,
        boolean_field_2.html_field_name: True,
        text_field.html_field_name: 'meow',
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[boolean_field_2.id].data
    assert text_field.id not in reg.data_by_field  # disabled conditional field cannot have data
    db.session.delete(reg)
    db.session.flush()

    # Same as above, but omitting the data. This should also not satisfy the text field condition since it
    # expects `False` ("No") for the boolean field, but no value is present.
    data = {
        **personal_data,
        boolean_field.html_field_name: True,
        text_field.html_field_name: 'meow',
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert reg.data_by_field[boolean_field_2.id].data is None
    assert text_field.id not in reg.data_by_field  # disabled conditional field cannot have data
    db.session.delete(reg)
    db.session.flush()

    # With both fields having the correct value, the text field should be stored now
    data = {
        **personal_data,
        boolean_field.html_field_name: True,
        boolean_field_2.html_field_name: False,
        text_field.html_field_name: 'meow',
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert not reg.data_by_field[boolean_field_2.id].data
    assert reg.data_by_field[text_field.id].data == 'meow'


@pytest.mark.usefixtures('request_context')
def test_modify_registration_conditional(monkeypatch, dummy_user, dummy_regform):
    monkeypatch.setattr('indico.modules.users.util.get_user_by_email', lambda *args, **kwargs: dummy_user)

    # Extend the dummy_regform with more sections and fields
    section = RegistrationFormSection(registration_form=dummy_regform, title='dummy_section', is_manager_only=False)

    boolean_field = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(boolean_field, {
        'input_type': 'bool', 'default_value': False, 'title': 'Bool 1'
    })
    db.session.flush()

    boolean_field_2 = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(boolean_field_2, {
        'input_type': 'bool', 'title': 'Bool 2',
        'show_if_field_id': boolean_field.id,
        'show_if_field_values': [True],
    })
    db.session.flush()

    text_field = RegistrationFormField(parent=section, registration_form=dummy_regform)
    _fill_form_field_with_data(text_field, {
        'input_type': 'text', 'title': 'Cond Text',
        'show_if_field_id': boolean_field_2.id,
        'show_if_field_values': [False],
    })
    db.session.flush()

    personal_data = {'email': dummy_user.email, 'first_name': dummy_user.first_name, 'last_name': dummy_user.last_name}

    # Register with the field having data
    data = {
        **personal_data,
        boolean_field.html_field_name: True,
        boolean_field_2.html_field_name: False,
        text_field.html_field_name: 'meow',
    }
    reg = create_registration(dummy_regform, data, invitation=None, management=False, notify_user=False)

    assert reg.data_by_field[boolean_field.id].data
    assert not reg.data_by_field[boolean_field_2.id].data
    assert reg.data_by_field[text_field.id].data == 'meow'

    # Modify the registration for the text field to become hidden
    data = {
        boolean_field.html_field_name: False,
    }
    modify_registration(reg, data, management=False, notify_user=False)

    assert not reg.data_by_field[boolean_field.id].data
    assert boolean_field_2.id not in reg.data_by_field  # data is deleted for hidden field
    assert text_field.id not in reg.data_by_field  # data is deleted for hidden field

    # Modify the registration and try setting value for hidden field
    data = {
        text_field.html_field_name: 'meow',
    }
    modify_registration(reg, data, management=False, notify_user=False)

    assert not reg.data_by_field[boolean_field.id].data
    assert boolean_field_2.id not in reg.data_by_field
    assert text_field.id not in reg.data_by_field

    # Modify the registration and try setting value for hidden field while still failing the conditions
    data = {
        boolean_field.html_field_name: False,
        boolean_field_2.html_field_name: True,  # should not be set
        text_field.html_field_name: 'meow',
    }
    modify_registration(reg, data, management=False, notify_user=False)

    assert not reg.data_by_field[boolean_field.id].data
    assert boolean_field_2.id not in reg.data_by_field
    assert text_field.id not in reg.data_by_field
