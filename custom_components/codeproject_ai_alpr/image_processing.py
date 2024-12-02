import logging
import requests
import voluptuous as vol
import io
import json

from PIL import Image, ImageDraw
from pathlib import Path

from homeassistant.components.image_processing import (
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SOURCE,
    PLATFORM_SCHEMA,
    ImageProcessingEntity,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import split_entity_id
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
from homeassistant.util.pil import draw_box

_LOGGER = logging.getLogger(__name__)

EVENT_VEHICLE_DETECTED = "codeproject_ai_alpr.vehicle_detected"

ATTR_PLATE = "plate"
ATTR_CONFIDENCE = "confidence"
ATTR_BOX_Y_CENTRE = "box_y_centre"
ATTR_BOX_X_CENTRE = "box_x_centre"

CONF_SAVE_FILE_FOLDER = "save_file_folder"
CONF_SAVE_TIMESTAMPTED_FILE = "save_timestamped_file"
CONF_ALWAYS_SAVE_LATEST_FILE = "always_save_latest_file"
CONF_SERVER = "server"
CONF_UNIQUE_ID = "unique_id"

DATETIME_FORMAT = "%Y-%m-%d_%H-%M-%S"
RED = (255, 0, 0)  # For bounding box color

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_SERVER): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_SAVE_FILE_FOLDER): cv.isdir,
        vol.Optional(CONF_SAVE_TIMESTAMPTED_FILE, default=False): cv.boolean,
        vol.Optional(CONF_ALWAYS_SAVE_LATEST_FILE, default=False): cv.boolean,
    }
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the platform."""
    save_file_folder = config.get(CONF_SAVE_FILE_FOLDER)
    if save_file_folder:
        save_file_folder = Path(save_file_folder)

    entities = []
    for camera in config[CONF_SOURCE]:
        entity = CodeProjectAIALPREntity(
            save_file_folder=save_file_folder,
            save_timestamped_file=config.get(CONF_SAVE_TIMESTAMPTED_FILE),
            always_save_latest_file=config.get(CONF_ALWAYS_SAVE_LATEST_FILE),
            camera_entity=camera[CONF_ENTITY_ID],
            name=camera.get(CONF_NAME),
            server=config.get(CONF_SERVER),
            unique_id=config.get(CONF_UNIQUE_ID),
        )
        entities.append(entity)
    add_entities(entities)


class CodeProjectAIALPREntity(ImageProcessingEntity):
    """Image processing entity for CodeProject AI ALPR."""

    def __init__(
        self,
        save_file_folder,
        save_timestamped_file,
        always_save_latest_file,
        camera_entity,
        name,
        server,
        unique_id,
    ):
        """Initialize the entity."""
        self._camera = camera_entity
        self._name = name or f"codeproject_ai_alpr_{split_entity_id(camera_entity)[1]}"
        self._save_file_folder = save_file_folder
        self._save_timestamped_file = save_timestamped_file
        self._always_save_latest_file = always_save_latest_file
        self._server = server
        self._unique_id = unique_id
        self._state = None
        self._results = []
        self._vehicles = []
        self._last_detection = None
        self._image_width = None
        self._image_height = None
        self._image = None
        self._inference_time = None

    def process_image(self, image):
        """Process an image."""
        self._state = None
        self._results = []
        self._vehicles = []
        self._image = Image.open(io.BytesIO(bytearray(image)))
        self._image_width, self._image_height = self._image.size

        try:
            response = requests.post(
                self._server,
                files={"upload": image},
            ).json()
            self._results = response.get("predictions", [])
            self._inference_time = response.get("inferenceMs")
            self._vehicles = [
                {
                    ATTR_PLATE: r["plate"],
                    ATTR_CONFIDENCE: r["confidence"],
                    ATTR_BOX_Y_CENTRE: (r["y_min"] + ((r["y_max"] - r["y_min"]) / 2)),
                    ATTR_BOX_X_CENTRE: (r["x_min"] + ((r["x_max"] - r["x_min"]) / 2)),
                }
                for r in self._results
            ]
        except Exception as exc:
            _LOGGER.error("CodeProject AI ALPR error: %s", exc)

        self._state = len(self._vehicles)
        if self._state > 0:
            self._last_detection = dt_util.now().strftime(DATETIME_FORMAT)
            for vehicle in self._vehicles:
                self.fire_vehicle_detected_event(vehicle)

        if self._save_file_folder and (self._state > 0 or self._always_save_latest_file):
            self.save_image()

    def fire_vehicle_detected_event(self, vehicle):
        """Send an event when a vehicle is detected."""
        vehicle_copy = vehicle.copy()
        vehicle_copy.update({ATTR_ENTITY_ID: self.entity_id})
        self.hass.bus.fire(EVENT_VEHICLE_DETECTED, vehicle_copy)

    def save_image(self):
        """Save the image with bounding boxes around plates."""
        draw = ImageDraw.Draw(self._image)
        for vehicle in self._results:
            box = (
                round(vehicle["y_min"] / self._image_height, 3),
                round(vehicle["x_min"] / self._image_width, 3),
                round(vehicle["y_max"] / self._image_height, 3),
                round(vehicle["x_max"] / self._image_width, 3),
            )
            draw_box(draw, box, self._image_width, self._image_height, text=vehicle["plate"], color=RED)

        latest_save_path = self._save_file_folder / f"{self._name}_latest.png"
        self._image.save(latest_save_path)

        if self._save_timestamped_file:
            timestamp_save_path = self._save_file_folder / f"{self._name}_{self._last_detection}.png"
            self._image.save(timestamp_save_path)
            _LOGGER.info("Saved timestamped file: %s", timestamp_save_path)

    @property
    def camera_entity(self):
        """Return the camera entity ID."""
        return self._camera

    @property
    def name(self):
        """Return the name of the entity."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the entity."""
        return self._unique_id

    @property
    def should_poll(self):
        """Indicate that polling is not needed."""
        return False

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "plates"

    @property
    def extra_state_attributes(self):
        """Return the attributes of the entity."""
        attr = {
            "last_detection": self._last_detection,
            "vehicles": self._vehicles,
            "detected_plates": [r["plate"] for r in self._results],
            "inference_time": self._inference_time,
        }
        if self._save_file_folder:
            attr.update({
                CONF_SAVE_FILE_FOLDER: str(self._save_file_folder),
                CONF_SAVE_TIMESTAMPTED_FILE: self._save_timestamped_file,
                CONF_ALWAYS_SAVE_LATEST_FILE: self._always_save_latest_file,
            })
        return attr
