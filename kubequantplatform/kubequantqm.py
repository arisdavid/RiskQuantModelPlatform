import logging
import time
import boto3
import uuid
import json
import os
from modelorchestrator.model_orchestrator import ModelOrchestrator

from collections import namedtuple

logging.basicConfig(level=logging.INFO)

namespace = "kubeq"
queue_wait_time = 10
queue_name = 'data-ingestion-queue'
region_name = 'us-east-1'
vacant_job_slot = 10
max_queue_message_read = 10

SQSStatus = namedtuple("QueueStatus",
                       "messages_processed")


# TODO: This won't be required if the Vault is injected as configmap to the container envar
def aws_credentials_init():

    with open("vault/secrets/aws", "r") as aws:
        aws_secrets = aws.readlines()

    for aws_secret in aws_secrets:
        secret = aws_secret.strip()
        os.environ[secret.split("=")[0]] = secret.split("=")[1]

    return True


logging.info(os.environ)


class K8sParameters:

    _image_pull_policy = "Never"
    _restart_policy = "Never"

    def __init__(self, model_name):
        self.model_name = model_name

        # args = [bucket_name, file_name]
        args = [1000000, 900000, 500000, 0.18, 0.12]

        if model_name == "kmv":
            self.container_name = "credit-models"
            self.container_image = "credit-models:latest"
            self.container_args = args

            self.pod_name = f"cm-{uuid.uuid4()}"
            self.pod_labels = dict(name="credit-models", type="pod")

            self.job_name = f"cm-{uuid.uuid4()}"
            self.job_labels = dict(name="credit-models", type="job")

        else:
            self.container_name = "credit-models"
            self.container_image = "credit-models:latest"
            self.container_args = args

            self.pod_name = f"cm-{uuid.uuid4()}"
            self.pod_labels = dict(name="credit-models", type="pod")

            self.job_name = f"cm-{uuid.uuid4()}"
            self.job_labels = dict(name="credit-models", type="job")

    def make_parameters(self):

        parameters = dict(
            container=dict(
                name=self.container_name,
                image_pull_policy=self._image_pull_policy,
                image=self.container_image,
                args=self.container_args,
            ),
            pod=dict(
                name=self.pod_name,
                restart_policy=self._restart_policy,
                labels=self.pod_labels,
            ),
            job=dict(name=self.job_name, labels=self.job_labels,),
        )

        return parameters

    @property
    def parameters(self):
        return self.parameters


def call_worker(message):

    message_body = json.loads(message.body)

    if "Records" in message_body:
        if "s3" in message_body["Records"][0]:
            bucket_name = message_body["Records"][0]["s3"]["bucket"]["name"]

            model_name = bucket_name.split("-")[-1]
            params = K8sParameters(model_name)

            # Call Model Manager
            kube_object = ModelOrchestrator(
                namespace,
                params.get("container"),
                params.get("pod"),
                params.get("job"),
            )

            # kube_object.create_namespace()
            kube_object.launch_worker()

            job_status = None
            logging.info("Waiting for job to complete.")
            while job_status is None:
                job_status = kube_object.get_job_status()
            logging.info("Job complete.")
            kube_object.delete_old_jobs()
            kube_object.delete_old_pods()

    return logging.info(f"Worker completed processing message ID: {message.message_id}")


class KubeQuantQueueManager:

    def __init__(self):
        sqs = boto3.resource("sqs", region_name=region_name)
        self.queue = sqs.get_queue_by_name(QueueName=queue_name)
        self.queue_name = queue_name

    def process_message_queue(self):

        self.queue.reload()
        num_messages = int(self.queue.attributes["ApproximateNumberOfMessages"])
        logging.info(f'Number of messages in the queue: {num_messages}')

        messages_to_read = min(vacant_job_slot, max_queue_message_read)

        if num_messages > 0:
            # SQS does not guarantee the number of messages to be returned.
            # But it can't be more than MaxNumberOfMessages
            messages = self.queue.receive_messages(
                MaxNumberOfMessages=messages_to_read, WaitTimeSeconds=queue_wait_time)
            logging.info(f"Received {len(messages)} messages to process.")

            messages_processed = 0
            for message in messages:

                # Start doing some work on this message
                call_worker(message)

                messages_processed += 1
                self.queue.delete_messages(
                    Entries=[
                        {
                            "Id": message.message_id,
                            "ReceiptHandle": message.receipt_handle,
                        }
                    ]
                )

            return SQSStatus(messages_processed)

        else:

            return SQSStatus(0)


if __name__ == "__main__":

    try:
        is_aws_available = aws_credentials_init()
    except FileNotFoundError:
        is_aws_available = 1
        logging.info("Using credentials found on the environment.")
    else:
        logging.info("Using credentials injected from vault.")

    if is_aws_available:
        region_name = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        sqs_queue_manager = KubeQuantQueueManager()
        while True:
            results = sqs_queue_manager.process_message_queue()
            if results.messages_processed is None or results.messages_processed == 0:
                time.sleep(queue_wait_time)
    else:
        raise Exception("AWS credentials missing. Unable to start application.")
