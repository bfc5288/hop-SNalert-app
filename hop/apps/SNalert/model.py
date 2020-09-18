#!/usr/bin/env python

import argparse
import datetime
import logging
import os
import sys
import time
import uuid

from dotenv import load_dotenv
import jsonschema
from jsonschema import validate
import numpy

from hop import Stream
from hop import auth
from hop.auth import Auth
from hop.plugins.snews import SNEWSAlert, SNEWSHeartbeat, SNEWSObservation

from . import decider
from . import msgSchema


logger = logging.getLogger("snews")


def _add_parser_args(parser):
    """Parse arguments for broker, configurations and options
    """
    parser.add_argument('-v', '--verbose', action='count', default=0, help="Be verbose.")
    parser.add_argument('-f', '--env-file', type=str, help="The path to the .env file.")
    parser.add_argument('--use-default-auth', action="store_true",
                        help='If set, use local ~/.config/hop-client/config.toml file to authenticate.')
    parser.add_argument("--no-auth", action="store_true", help="If set, disable authentication.")


# verify json
def validateJson(jsonData, jsonSchema):
    """
    Function for validate a json data using a json schema.
    :param jsonData: the data to validate.
    :param jsonSchema: the schema assumed to be correct.
    :return: true or false
    """
    try:
        validate(instance=jsonData, schema=jsonSchema)
    except jsonschema.exceptions.ValidationError as err:
        return False
    return True


class Model(object):
    def __init__(self, args):
        """
        The constructor of the model class.
        :param args: the command line arguments
        """
        # load environment variables
        load_dotenv(dotenv_path=args.env_file)

        self.args = args
        self.gcnFormat = "json"
        self.db_server = os.getenv("DATABASE_SERVER")
        self.drop_db = bool(os.getenv("NEW_DATABASE"))
        self.regularMsgSchema = msgSchema.regularMsgSchema

        logger.info(f"setting up decider at: {self.db_server}")
        self.myDecider = decider.Decider(
            int(os.getenv("TIMEOUT")),
            os.getenv("TIME_STRING_FORMAT"),
            self.db_server,
            self.drop_db
        )
        if self.drop_db:
            logger.info("clearing out decider cache")
        self.deciderUp = False

        # configure authentication
        if args.no_auth:
            self.auth = False
        elif not args.use_default_auth:
            username = os.getenv("USERNAME")
            password = os.getenv("PASSWORD")
            self.auth = Auth(username, password, method=auth.SASLMethod.PLAIN)
        else:
            self.auth = True

        # specify topics
        self.observation_topic = os.getenv("OBSERVATION_TOPIC")
        self.alert_topic = os.getenv("ALERT_TOPIC")

        # open up stream connections
        self.stream = Stream(auth=self.auth, persist=True)
        self.source = self.stream.open(self.observation_topic, "r")
        self.sink = self.stream.open(self.alert_topic, "w")

        # message types and processing algorithms
        self.mapping = {
            SNEWSObservation.__name__: self.processObservationMessage,
            SNEWSHeartbeat.__name__: self.processHeartbeatMessage
        }

    def run(self):
        """
        Execute the model.
        :return: none
        """
        self.deciderUp = True
        logger.info(f"starting decider")
        logger.info(f"processing messages from {self.observation_topic}")
        for msg, meta in self.source.read(metadata=True, autocommit=False):
            self.processMessage(msg)
            self.source.mark_done(meta)

    def close(self):
        """
        Close stream connections.
        """
        logger.info(f"shutting down")
        self.deciderUp = False
        self.source.close()
        self.sink.close()

    def addObservationMsg(self, message):
        self.myDecider.addMessage(message)

    def processMessage(self, message):
        message_type = type(message).__name__
        logger.debug(f"processing {message_type}")
        if message_type in self.mapping:
            self.mapping[message_type](message)

    def processObservationMessage(self, message):
        self.addObservationMsg(message)
        alert = self.myDecider.deciding()
        if alert:
            # publish to TOPIC2 and alert astronomers
            logger.info("found coincidence, sending alert")
            self.sink.write(self.writeAlertMsg())

    def processHeartbeatMessage(self, message):
        pass
    
    def writeAlertMsg(self):
        return SNEWSAlert(
            message_id=str(uuid.uuid4()),
            sent_time=datetime.datetime.utcnow().strftime(os.getenv("TIME_STRING_FORMAT")),
            machine_time=datetime.datetime.utcnow().strftime(os.getenv("TIME_STRING_FORMAT")),
            content="Supernova Alert",
        )


def main(args):
    """main function
    """
    # set up logging
    verbosity = [logging.WARNING, logging.INFO, logging.DEBUG]
    logging.basicConfig(
        level=verbosity[min(args.verbose, 2)],
        format="%(asctime)s | model : %(levelname)s : %(message)s",
    )

    # start up
    model = Model(args)
    try:
        model.run()
    except KeyboardInterrupt:
        pass
    finally:
        model.close()
