import logging
import typing
import warnings
import json
from typing import Dict, Text, Any, List, Tuple, Union, Optional, Set

from abc import ABC
from rasa_sdk import utils
from rasa_sdk.events import SlotSet, EventType, ActiveLoop
from rasa_sdk.interfaces import Action, ActionExecutionRejection
from rasa_sdk.slots import SlotMapping

logger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:  # pragma: no cover
    from rasa_sdk import Tracker
    from rasa_sdk.executor import CollectingDispatcher
    from rasa_sdk.types import DomainDict

# this slot is used to store information needed
# to do the form handling
REQUESTED_SLOT = "requested_slot"

LOOP_INTERRUPTED_KEY = "is_interrupted"

VALIDATE_GLOBAL_SLOT_MAPPINGS_NAME = "validate_global_slot_mappings"


class FormAction(Action):
    """An action which implements and executes the form logic."""

    def __init__(self):
        warnings.warn(
            "Using the `FormAction` class is deprecated as of Rasa Open "
            "Source version 2.0. Please see the migration guide "
            "for Rasa Open Source 2.0 for instructions how to migrate.",
            FutureWarning,
        )
        self._have_unique_entity_mappings_been_initialized = False
        super().__init__()

    def name(self) -> Text:
        """Unique identifier of the form"""

        raise NotImplementedError("A form must implement a name")

    @staticmethod
    def required_slots(tracker: "Tracker") -> List[Text]:
        """A list of required slots that the form has to fill.

        Use `tracker` to request different list of slots
        depending on the state of the dialogue
        """

        raise NotImplementedError(
            "A form must implement required slots that it has to fill"
        )

    # noinspection PyMethodMayBeStatic
    def get_mappings_for_slot(
        self, slot_to_fill: Text, domain: "DomainDict"
    ) -> List[Dict[Text, Any]]:
        """Get mappings for requested slot.

        If None, map requested slot to an entity with the same name
        """
        domain_slots = domain.get("slots")
        requested_slot_mappings = domain_slots.get(slot_to_fill).get("mappings")

        # check provided slot mappings
        for requested_slot_mapping in requested_slot_mappings:
            if (
                not isinstance(requested_slot_mapping, dict)
                or requested_slot_mapping.get("type") is None
            ):
                raise TypeError("Provided incompatible slot mapping")

        return requested_slot_mappings

    @staticmethod
    def get_entity_value(
        name: Text,
        tracker: "Tracker",
        role: Optional[Text] = None,
        group: Optional[Text] = None,
    ) -> Optional[Union[Text, List[Text]]]:
        """Extract entities for given name and optional role and group.

        Args:
            name: entity type (name) of interest
            tracker: the tracker
            role: optional entity role of interest
            group: optional entity group of interest

        Returns:
            Value of entity.
        """
        # list is used to cover the case of list slot type
        values = list(
            tracker.get_latest_entity_values(name, entity_group=group, entity_role=role)
        )
        if not values:
            return None

        if len(values) == 1:
            return values[0]

        return values

    # noinspection PyUnusedLocal
    def extract_other_slots(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> Dict[Text, Any]:
        """Extract the values of the other slots
        if they are set by corresponding entities from the user input
        else return None
        """
        slot_to_fill = tracker.get_slot(REQUESTED_SLOT)

        slot_values = {}
        for slot in self.required_slots(tracker):
            # look for other slots
            if slot != slot_to_fill:
                # list is used to cover the case of list slot type
                other_slot_mappings = self.get_mappings_for_slot(slot, domain)

                for other_slot_mapping in other_slot_mappings:
                    if not self._matches_mapping_conditions(
                        other_slot_mapping, slot_to_fill
                    ):
                        continue

                    # check whether the slot should be filled by an entity in the input
                    entity_is_desired = SlotMapping.entity_is_desired(
                        other_slot_mapping, tracker
                    ) and self._entity_mapping_is_unique(
                        other_slot_mapping, tracker, domain
                    )
                    should_fill_entity_slot = (
                        other_slot_mapping["type"] == str(SlotMapping.FROM_ENTITY)
                        and SlotMapping.intent_is_desired(
                            other_slot_mapping, tracker, domain
                        )
                        and entity_is_desired
                    )
                    # check whether the slot should be
                    # filled from trigger intent mapping
                    should_fill_trigger_slot = (
                        tracker.active_loop.get("name") != self.name()
                        and other_slot_mapping["type"]
                        == str(SlotMapping.FROM_TRIGGER_INTENT)
                        and SlotMapping.intent_is_desired(
                            other_slot_mapping, tracker, domain
                        )
                    )
                    if should_fill_entity_slot:
                        value = self.get_entity_value(
                            other_slot_mapping["entity"],
                            tracker,
                            other_slot_mapping.get("role"),
                            other_slot_mapping.get("group"),
                        )
                    elif should_fill_trigger_slot:
                        value = other_slot_mapping.get("value")
                    else:
                        value = None

                    if value is not None:
                        logger.debug(f"Extracted '{value}' for extra slot '{slot}'.")
                        slot_values[slot] = value
                        # this slot is done, check  next
                        break

        return slot_values

    def _entity_mapping_is_unique(
        self, slot_mapping: Dict[Text, Any], tracker: "Tracker", domain: "DomainDict"
    ) -> bool:
        if not self._have_unique_entity_mappings_been_initialized:
            # create unique entity mappings on the first call
            self._unique_entity_mappings = self._create_unique_entity_mappings(
                tracker, domain
            )
            self._have_unique_entity_mappings_been_initialized = True

        mapping_as_string = json.dumps(slot_mapping, sort_keys=True)
        return mapping_as_string in self._unique_entity_mappings

    def _create_unique_entity_mappings(
        self, tracker: "Tracker", domain: "DomainDict"
    ) -> Set[Text]:
        """Finds mappings of type `from_entity` that uniquely set a slot.

        For example in the following form:
        some_form:
          departure_city:
            - type: from_entity
              entity: city
              role: from
            - type: from_entity
              entity: city
          arrival_city:
            - type: from_entity
              entity: city
              role: to
            - type: from_entity
              entity: city

        An entity `city` with a role `from` uniquely sets the slot `departure_city`
        and an entity `city` with a role `to` uniquely sets the slot `arrival_city`,
        so corresponding mappings are unique.
        But an entity `city` without a role can fill both `departure_city`
        and `arrival_city`, so corresponding mapping is not unique.

        Args:
            domain: The domain.

        Returns:
            A set of json dumps of unique mappings of type `from_entity`.
        """
        unique_entity_slot_mappings = set()
        duplicate_entity_slot_mappings = set()
        domain_slots = domain.get("slots")
        for slot in self.required_slots(tracker):
            for slot_mapping in domain_slots.get(slot).get("mappings"):
                if slot_mapping.get("type") == str(SlotMapping.FROM_ENTITY):
                    mapping_as_string = json.dumps(slot_mapping, sort_keys=True)
                    if mapping_as_string in unique_entity_slot_mappings:
                        unique_entity_slot_mappings.remove(mapping_as_string)
                        duplicate_entity_slot_mappings.add(mapping_as_string)
                    elif mapping_as_string not in duplicate_entity_slot_mappings:
                        unique_entity_slot_mappings.add(mapping_as_string)

        return unique_entity_slot_mappings

    # noinspection PyUnusedLocal
    def extract_requested_slot(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        slot_to_fill: Any,
        domain: "DomainDict",
    ) -> Dict[Text, Any]:
        """Extract the value of requested slot from a user input
        else return None
        """
        logger.debug(f"Trying to extract requested slot '{slot_to_fill}' ...")

        # get mapping for requested slot
        requested_slot_mappings = self.get_mappings_for_slot(slot_to_fill, domain)

        for requested_slot_mapping in requested_slot_mappings:
            logger.debug(f"Got mapping '{requested_slot_mapping}'")

            if SlotMapping.intent_is_desired(requested_slot_mapping, tracker, domain):
                if not self._matches_mapping_conditions(
                    requested_slot_mapping, slot_to_fill
                ):
                    continue

                mapping_type = requested_slot_mapping["type"]

                if mapping_type == str(SlotMapping.FROM_ENTITY):
                    entity_type = requested_slot_mapping.get("entity")
                    value = (
                        self.get_entity_value(
                            entity_type,
                            tracker,
                            requested_slot_mapping.get("role"),
                            requested_slot_mapping.get("group"),
                        )
                        if entity_type
                        else None
                    )
                elif mapping_type == str(SlotMapping.FROM_INTENT):
                    value = requested_slot_mapping.get("value")
                elif mapping_type == str(SlotMapping.FROM_TRIGGER_INTENT):
                    # from_trigger_intent is only used on form activation
                    continue
                elif mapping_type == str(SlotMapping.FROM_TEXT):
                    value = tracker.latest_message.get("text")
                else:
                    raise ValueError("Provided slot mapping type is not supported")

                if value is not None:
                    logger.debug(
                        f"Successfully extracted '{value}' for requested slot '{slot_to_fill}'"
                    )
                    return {slot_to_fill: value}

        logger.debug(f"Failed to extract requested slot '{slot_to_fill}'")
        return {}

    async def validate_slots(
        self,
        slot_dict: Dict[Text, Any],
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Validate slots using helper validation functions.

        Call validate_{slot} function for each slot, value pair to be validated.
        If this function is not implemented, set the slot to the value.
        """

        for slot, value in list(slot_dict.items()):
            validate_func = getattr(self, f"validate_{slot}", lambda *x: {slot: value})
            validation_output = await utils.call_potential_coroutine(
                validate_func(value, dispatcher, tracker, domain)
            )
            if not isinstance(validation_output, dict):
                warnings.warn(
                    "Returning values in helper validation methods is deprecated. "
                    + f"Your `validate_{slot}()` method should return "
                    + "a dict of {'slot_name': value} instead."
                )
                validation_output = {slot: validation_output}
            slot_dict.update(validation_output)

        # validation succeed, set slots to extracted values
        return [SlotSet(slot, value) for slot, value in slot_dict.items()]

    async def validate(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Extract and validate value of requested slot.

        If nothing was extracted reject execution of the form action.
        Subclass this method to add custom validation and rejection logic
        """

        # extract other slots that were not requested
        # but set by corresponding entity or trigger intent mapping
        slot_values = self.extract_other_slots(dispatcher, tracker, domain)

        # extract requested slot
        slot_to_fill = tracker.get_slot(REQUESTED_SLOT)
        if slot_to_fill:
            slot_values.update(
                self.extract_requested_slot(dispatcher, tracker, slot_to_fill, domain)
            )

            if not slot_values:
                # reject to execute the form action
                # if some slot was requested but nothing was extracted
                # it will allow other policies to predict another action
                raise ActionExecutionRejection(
                    self.name(),
                    f"Failed to extract slot {slot_to_fill} with action {self.name()}."
                    f"Allowing other policies to predict next action.",
                )
        logger.debug(f"Validating extracted slots: {slot_values}")
        return await self.validate_slots(slot_values, dispatcher, tracker, domain)

    # noinspection PyUnusedLocal
    def request_next_slot(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> Optional[List[EventType]]:
        """Request the next slot and utter template if needed,
        else return None"""

        for slot in self.required_slots(tracker):
            if self._should_request_slot(tracker, slot):
                logger.debug(f"Request next slot '{slot}'")
                dispatcher.utter_message(template=f"utter_ask_{slot}", **tracker.slots)
                return [SlotSet(REQUESTED_SLOT, slot)]

        # no more required slots to fill
        return None

    def deactivate(self) -> List[EventType]:
        """Return `Form` event with `None` as name to deactivate the form
        and reset the requested slot"""

        logger.debug(f"Deactivating the form '{self.name()}'")
        return [ActiveLoop(None), SlotSet(REQUESTED_SLOT, None)]

    async def submit(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Define what the form has to do
        after all required slots are filled"""

        raise NotImplementedError("A form must implement a submit method")

    # helpers
    def _matches_mapping_conditions(
        self, mapping: Dict[Text, Any], slot_to_fill: Optional[Text]
    ) -> bool:
        slot_mapping_conditions = mapping.get("conditions")

        # check if found mapping conditions matches form
        if slot_mapping_conditions:
            for i, condition in enumerate(slot_mapping_conditions):
                active_loop = condition.get("active_loop")

                if active_loop == self.name():
                    condition_requested_slot = condition.get(REQUESTED_SLOT)
                    if (
                        condition_requested_slot
                        and condition_requested_slot != slot_to_fill
                    ):
                        return False
                    return True
                else:
                    if i == len(slot_mapping_conditions) - 1:
                        return False

        return True

    def _log_form_slots(self, tracker: "Tracker") -> None:
        """Logs the values of all required slots before submitting the form."""
        slot_values = "\n".join(
            [
                f"\t{slot}: {tracker.get_slot(slot)}"
                for slot in self.required_slots(tracker)
            ]
        )
        logger.debug(
            f"No slots left to request, all required slots are filled:\n{slot_values}"
        )

    async def _activate_if_required(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Activate form if the form is called for the first time.

        If activating, validate any required slots that were filled before
        form activation and return `Form` event with the name of the form, as well
        as any `SlotSet` events from validation of pre-filled slots.
        """

        if tracker.active_loop.get("name") is not None:
            logger.debug(f"The form '{tracker.active_loop}' is active")
        else:
            logger.debug("There is no active form")

        if tracker.active_loop.get("name") == self.name():
            return []
        else:
            logger.debug(f"Activated the form '{self.name()}'")
            events = [ActiveLoop(self.name())]

            # collect values of required slots filled before activation
            prefilled_slots = {}

            for slot_name in self.required_slots(tracker):
                if not self._should_request_slot(tracker, slot_name):
                    prefilled_slots[slot_name] = tracker.get_slot(slot_name)

            if prefilled_slots:
                logger.debug(f"Validating pre-filled required slots: {prefilled_slots}")
                events.extend(
                    await self.validate_slots(
                        prefilled_slots, dispatcher, tracker, domain
                    )
                )
            else:
                logger.debug("No pre-filled required slots to validate.")

            return events

    async def _validate_if_required(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Return a list of events from `self.validate(...)`
        if validation is required:
        - the form is active
        - the form is called after `action_listen`
        - form validation was not cancelled
        """
        # no active_loop means that it is called during activation
        need_validation = not tracker.active_loop or (
            tracker.latest_action_name == "action_listen"
            and not tracker.active_loop.get(LOOP_INTERRUPTED_KEY, False)
        )
        if need_validation:
            logger.debug(f"Validating user input '{tracker.latest_message}'")
            return await utils.call_potential_coroutine(
                self.validate(dispatcher, tracker, domain)
            )
        else:
            logger.debug("Skipping validation")
            return []

    @staticmethod
    def _should_request_slot(tracker: "Tracker", slot_name: Text) -> bool:
        """Check whether form action should request given slot"""

        return tracker.get_slot(slot_name) is None

    async def run(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Execute the side effects of this form.

        Steps:
        - activate if needed
        - validate user input if needed
        - set validated slots
        - utter_ask_{slot} template with the next required slot
        - submit the form if all required slots are set
        - deactivate the form
        """

        # activate the form
        events = await self._activate_if_required(dispatcher, tracker, domain)
        # validate user input
        events.extend(await self._validate_if_required(dispatcher, tracker, domain))
        # check that the form wasn't deactivated in validation
        if ActiveLoop(None) not in events:

            # create temp tracker with populated slots from `validate` method
            temp_tracker = tracker.copy()
            for e in events:
                if e["event"] == "slot":
                    temp_tracker.slots[e["name"]] = e["value"]

            next_slot_events = self.request_next_slot(dispatcher, temp_tracker, domain)

            if next_slot_events is not None:
                # request next slot
                events.extend(next_slot_events)
            else:
                # there is nothing more to request, so we can submit
                self._log_form_slots(temp_tracker)
                logger.debug(f"Submitting the form '{self.name()}'")
                events += await utils.call_potential_coroutine(
                    self.submit(dispatcher, temp_tracker, domain)
                )

                # deactivate the form after submission
                events += await utils.call_potential_coroutine(self.deactivate())

        return events

    def __str__(self) -> Text:
        return f"FormAction('{self.name()}')"


class ValidationAction(Action, ABC):
    """A helper class for slot validations and extractions of custom slots."""

    def name(self) -> Text:
        """Unique identifier of this simple action."""
        return VALIDATE_GLOBAL_SLOT_MAPPINGS_NAME

    async def run(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Runs the custom actions. Please the docstring of the parent class."""
        extraction_events = await self.get_extraction_events(
            dispatcher, tracker, domain
        )
        tracker.add_slots(extraction_events)

        validation_events = await self.get_validation_events(
            dispatcher, tracker, domain
        )
        tracker.add_slots(validation_events)

        next_slot = await self.next_requested_slot(dispatcher, tracker, domain)
        if next_slot:
            validation_events.append(next_slot)

        # Validation events include events for extracted slots
        return validation_events

    async def required_slots(
        self,
        domain_slots: List[Text],
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[Text]:
        """Returns slots which the validation action should fill.

        Args:
            domain_slots: Names of slots of this form which were mapped in
                the domain.
            dispatcher: the dispatcher which is used to
                send messages back to the user.
            tracker: the conversation tracker for the current user.
            domain: the bot's domain.

        Returns:
            A list of slot names.
        """
        return domain_slots

    async def get_extraction_events(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Extracts custom slots using available `extract_<slot name>` methods.

        Uses the information from `self.required_slots` to gather which slots should
        be extracted.

        Args:
            dispatcher: the dispatcher which is used to
                send messages back to the user. Use
                `dispatcher.utter_message()` for sending messages.
            tracker: the state tracker for the current
                user. You can access slot values using
                `tracker.get_slot(slot_name)`, the most recent user message
                is `tracker.latest_message.text` and any other
                `rasa_sdk.Tracker` property.
            domain: the bot's domain.

        Returns:
            `SlotSet` for any extracted slots.
        """
        custom_slots = {}
        slots_to_extract = await self.required_slots(
            self.domain_slots(domain), dispatcher, tracker, domain
        )

        for slot in slots_to_extract:
            extraction_output = await self._extract_slot(
                slot, dispatcher, tracker, domain
            )
            custom_slots.update(extraction_output)
            # for sequential consistency, also update tracker
            # to make changes visible to subsequent extract_{slot_name}
            tracker.slots.update(extraction_output)

        return [SlotSet(slot, value) for slot, value in custom_slots.items()]

    async def get_validation_events(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> List[EventType]:
        """Validate slots by calling a validation function for each slot.

        Args:
            dispatcher: the dispatcher which is used to
                send messages back to the user.
            tracker: the conversation tracker for the current user.
            domain: the bot's domain.
        Returns:
            `SlotSet` events for every validated slot.
        """
        slots_to_validate = await self.required_slots(
            self.domain_slots(domain), dispatcher, tracker, domain
        )
        slots: Dict[Text, Any] = tracker.slots_to_validate()

        for slot_name, slot_value in list(slots.items()):
            if slot_name not in slots_to_validate:
                slots.pop(slot_name)
                continue

            method_name = f"validate_{slot_name.replace('-','_')}"
            validate_method = getattr(self, method_name, None)

            if not validate_method:
                logger.warning(
                    f"Skipping validation for `{slot_name}`: there is no validation "
                    f"method specified."
                )
                continue

            validation_output = await utils.call_potential_coroutine(
                validate_method(slot_value, dispatcher, tracker, domain)
            )

            if isinstance(validation_output, dict):
                slots.update(validation_output)
                # for sequential consistency, also update tracker
                # to make changes visible to subsequent validate_{slot_name}
                tracker.slots.update(validation_output)
            else:
                warnings.warn(
                    f"Cannot validate `{slot_name}`: make sure the validation method "
                    f"returns the correct output."
                )

        return [SlotSet(slot, value) for slot, value in slots.items()]

    async def next_requested_slot(
        self,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> Optional[EventType]:
        """Sets the next slot which should be requested.

        Skips setting the next requested slot in case `missing_slots` was not
        overridden.

        Args:
            dispatcher: the dispatcher which is used to
                send messages back to the user.
            tracker: the conversation tracker for the current user.
            domain: the bot's domain.

        Returns:
            `None` in case `missing_slots` was not overridden and returns `None`.
            Otherwise returns a `SlotSet` event for the next slot to be requested.
            If the `SlotSet` event sets `requested_slot` to `None`, the form will be
            deactivated.
        """
        required_slots = await self.required_slots(
            self.domain_slots(domain), dispatcher, tracker, domain
        )
        if required_slots == self.domain_slots(domain):
            # If users didn't override `required_slots` then we'll let the `FormAction`
            # within Rasa Open Source request the next slot.
            return None

        missing_slots = (
            slot_name
            for slot_name in required_slots
            if tracker.slots.get(slot_name) is None
        )

        return SlotSet(REQUESTED_SLOT, next(missing_slots, None))

    @staticmethod
    def _is_mapped_to_form(slot_value: Dict[Text, Any]) -> bool:
        mappings = slot_value.get("mappings")
        if not mappings:
            return False

        for mapping in mappings:
            mapping_conditions = mapping.get("conditions", [])
            for condition in mapping_conditions:
                if condition.get("active_loop"):
                    return True

        return False

    def global_slots(self, domain: "DomainDict") -> List[Text]:
        """Returns all slots that contain no form condition."""
        all_slots = domain.get("slots", {})
        return [k for k, v in all_slots.items() if not self._is_mapped_to_form(v)]

    def domain_slots(self, domain: "DomainDict") -> List[Text]:
        """Returns slots which were mapped in the domain.

        Args:
            domain: The current domain.

        Returns:
            Slot names mapped in the domain which do not include
            a mapping with an active loop condition.
        """
        return self.global_slots(domain)

    async def _extract_slot(
        self,
        slot_name: Text,
        dispatcher: "CollectingDispatcher",
        tracker: "Tracker",
        domain: "DomainDict",
    ) -> Dict[Text, Any]:
        method_name = f"extract_{slot_name.replace('-', '_')}"

        slot_in_domain = slot_name in self.domain_slots(domain)
        extract_method = getattr(self, method_name, None)

        if not extract_method:
            if not slot_in_domain:
                warnings.warn(
                    f"No method '{method_name}' found for slot "
                    f"'{slot_name}'. Skipping extraction for this slot."
                )
            return {}

        if extract_method and slot_in_domain:
            warnings.warn(
                f"Slot '{slot_name}' is mapped in the domain and your custom "
                f"action defines '{method_name}'. '{method_name}' will override any "
                f"extractions of the predefined slot mapping from the domain. It is "
                f"suggested to define a slot mapping in only one of the two ways for "
                f"clarity."
            )

        extracted = await utils.call_potential_coroutine(
            extract_method(dispatcher, tracker, domain)
        )

        if isinstance(extracted, dict):
            return extracted

        warnings.warn(
            f"Cannot extract `{slot_name}`: make sure the extract method "
            f"returns the correct output."
        )
        return {}


class FormValidationAction(ValidationAction, ABC):
    """A helper class for slot validations and extractions of custom slots in forms."""

    def name(self) -> Text:
        """Unique identifier of this simple action."""
        raise NotImplementedError("An action must implement a name")

    def form_name(self) -> Text:
        """Returns the form's name."""
        return self.name().replace("validate_", "", 1)

    def domain_slots(self, domain: "DomainDict") -> List[Text]:
        """Returns slots which were mapped in the domain.

        Args:
            domain: The current domain.

        Returns:
            Slot names which should be filled by the form. By default it
            returns the slot names which are listed for this form in the domain
            and use predefined mappings.
        """
        form = domain.get("forms", {}).get(self.form_name(), {})
        if "required_slots" in form:
            return form.get("required_slots", [])
        return []
