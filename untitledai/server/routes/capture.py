#
# capture.py
#
# Capture endpoints: streaming and chunked file uploads via HTTP handled here.
#

from datetime import datetime, timedelta, timezone
from glob import glob
import os
from typing import Annotated
import uuid

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, UploadFile, Form, Depends
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect
from sqlmodel import Session
import logging
import traceback

from .. import AppState
from ..task import Task
from ...database.crud import create_location
from ...files import CaptureFile, append_to_wav_file
from ...models.schemas import Location, ConversationProgress
from ..streaming_capture_handler import StreamingCaptureHandler
from ...services import ConversationDetectionService


logger = logging.getLogger(__name__)

router = APIRouter()


####################################################################################################
# Stream API
####################################################################################################

@router.post("/capture/streaming_post/{capture_uuid}")
async def streaming_post(request: Request, capture_uuid: str, device_type: str, app_state: AppState = Depends(AppState.authenticate_request)):
    logger.info('Client connected')
    try:
        if capture_uuid not in app_state.capture_handlers:
            app_state.capture_handlers[capture_uuid] = StreamingCaptureHandler(app_state, device_type, capture_uuid, file_extension = "wav")

        capture_handler = app_state.capture_handlers[capture_uuid]

        async for chunk in request.stream():
            await capture_handler.handle_audio_data(chunk)

    except ClientDisconnect:
        logger.info(f"Client disconnected while streaming {capture_uuid}.")

    return JSONResponse(content={"message": f"Audio received"})


@router.post("/capture/streaming_post/{capture_uuid}/complete")
async def complete_audio(request: Request, background_tasks: BackgroundTasks, capture_uuid: str, app_state: AppState = Depends(AppState.authenticate_request)):
    logger.info(f"Completing audio capture for {capture_uuid}")
    if capture_uuid not in app_state.capture_handlers:
        logger.error(f"Capture session not found: {capture_uuid}")
        raise HTTPException(status_code=500, detail="Capture session not found")
    capture_handler = app_state.capture_handlers[capture_uuid]
    capture_handler.finish_capture_session()

    return JSONResponse(content={"message": f"Audio processed"})


####################################################################################################
# Chunk API
####################################################################################################

supported_upload_file_extensions = set([ "pcm", "wav", "aac" ])

class ProcessAudioChunkTask(Task):
    """
    Processes the newest chunk of audio in a capture. Detects conversations incrementally and
    processes any that are found.
    """

    def __init__(
        self,
        capture_file: CaptureFile,
        detection_service: ConversationDetectionService,
        format: str,
        audio_data: bytes | None = None
    ):
        self._capture_file = capture_file
        self._detection_service = detection_service
        self._audio_data = audio_data
        self._format = format
        assert format == "wav" or format == "aac"

    async def run(self, app_state: AppState):
        # Data we need
        capture_file = self._capture_file
        audio_data = self._audio_data
        capture_finished = audio_data is None
        format = self._format
        detection_service = self._detection_service
        
        # Run conversation detection stage (finds conversations thus far)
        detection_results = await detection_service.detect_conversations(audio_data=audio_data, format=format, capture_finished=capture_finished)

        #
        # TODO for Ethan:
        #
        # - Suggested states for Conversation object (to unify with ConversationInProgress):
        #       - RECORDING / CAPTURING
        #       - PROCESSING
        #       - COMPLETED
        #       - FAILED_PROCESSING? <-- For now we don't have a way to set this and I wouldn't add it yet but could be added in future
        # - We should also log last update time in schema. Right now, end time is the end of the last VAD segment. So if you go silent,
        #   this doesn't update. Also, it only updates at most every 30 seconds when chunking, so having a last update time could allow
        #   for a UI that gives more information as to whether anything is still being received or whether we are stuck. We can do the UI
        #   stuff later.
        #
        # - detection_results holds all completed and possibly an in-progress conversation. I process
        #   them all together at the *bottom of this function*. But we should start doing stuff here.
        #
        # - At this point detection_results.completed contains a list of *completed* conversations.
        #   We may have never encountered these yet in the case of passing in a large chunk. We 
        #   should create new database objects if needed, with an *in-processing* state (because
        #   completed convos are immediately processed next), or update existing objects if they
        #   exist (i.e., we had entered "recording" state into the database previously.)
        #
        # - Here is also where an in-progress conversation ("recording" state) can be first detected.
        #   It would be on detection_results.in_progress (when not None). Would be good to enter it
        #   into the database in a "recording" state and notify the server.
        #
        # - Then, after this, I have code that takes the completed conversations and extracts them
        #   into files and processes them....
        #

        # Create conversation segment files and store extracted conversations there
        convo_filepaths = []
        segment_files = []
        for convo in detection_results.completed:
            segment_file = capture_file.create_conversation_segment(
                conversation_uuid=convo.uuid,
                timestamp=convo.endpoints.start,
                file_extension=format
            )
            convo_filepaths.append(segment_file.filepath)
            segment_files.append(segment_file)
        await detection_service.extract_conversations(conversations=detection_results.completed, conversation_filepaths=convo_filepaths)

        # Process each completed conversation
        try:
            for segment_file in segment_files:
                await app_state.conversation_service.process_conversation_from_audio(
                    capture_file=capture_file,
                    segment_file=segment_file,
                    voice_sample_filepath=app_state.config.user.voice_sample_filepath,
                    speaker_name=app_state.config.user.name
                )
        except Exception as e:
            logging.error(f"Error processing conversation: {e}")

        #
        # TODO for Ethan:
        #
        # - Right now, as I said above, I actually handle all the progress updates here in one big pass.
        #   All completed convos and the in-progress one are handled here.
        #
        # - In reality, we would want to have only the completed conversations finalized here.
        #

        # Construct progress updates to server
        progress_updates = []

        # Inform the server that all completed conversations are no longer "in progress"
        for convo in detection_results.completed:
            progress = ConversationProgress(
                conversation_uuid=convo.uuid,
                in_conversation=False,
                start_time=convo.endpoints.start,
                end_time=convo.endpoints.end,
                device_type=capture_file.device_type.value
            )
            progress_updates.append(progress)

        # If there is an in-progress conversation, add that
        conversation_in_progress = detection_results.in_progress
        if conversation_in_progress is not None:
            progress = ConversationProgress(
                conversation_uuid=conversation_in_progress.uuid,
                in_conversation=True,
                start_time=conversation_in_progress.endpoints.start,
                end_time=conversation_in_progress.endpoints.end,
                device_type=capture_file.device_type.value
            )
            progress_updates.append(progress)
            
        # Send updates to server
        for progress in progress_updates:
            await app_state.notification_service.send_notification(
                title="New Conversation-in-Progress",
                body=f"On device: {capture_file.device_type.value}",
                type="conversation_progress",
                payload=progress.model_dump_json()
            )

Task.register(ProcessAudioChunkTask)

def find_audio_filepath(audio_directory: str, capture_uuid: str) -> str | None:
    # Files stored as: {audio_directory}/{date}/{device}/{files}.{ext}
    filepaths = glob(os.path.join(audio_directory, "*/*/*"))
    capture_uuids = [ CaptureFile.get_capture_uuid(filepath=filepath) for filepath in filepaths ]
    file_idx = capture_uuids.index(capture_uuid)
    if file_idx < 0:
        return None
    return filepaths[file_idx]

@router.post("/capture/upload_chunk")
async def upload_chunk(
    request: Request,
    file: UploadFile,
    capture_uuid: Annotated[str, Form()],
    timestamp: Annotated[str, Form()],
    device_type: Annotated[str, Form()],
    app_state: AppState = Depends(AppState.authenticate_request)
):
    try:
        # Validate file format
        file_extension = os.path.splitext(file.filename)[1].lstrip(".")
        if file_extension not in supported_upload_file_extensions:
            return JSONResponse(content={"message": f"Failed to process because file extension is unsupported: {file_extension}"})

        # Raw PCM is automatically converted to wave format. We do this to prevent client from
        # having to worry about reliability of transmission (in case WAV header chunk is dropped).
        write_wav_header = False
        if file_extension == "pcm":
            file_extension = "wav"
            write_wav_header = True

        # Look up capture session or create a new one
        capture_file: CaptureFile = None
        detection_service: ConversationDetectionService = None
        if capture_uuid in app_state.capture_files_by_id:
            capture_file = app_state.capture_files_by_id[capture_uuid]
            detection_service = app_state.conversation_detection_service_by_id.get(capture_uuid)
            if detection_service is None:
                logger.error(f"Internal error: No conversation detection service exists for capture_uuid={capture_uuid}")
                raise HTTPException(status_code=500, detail="Internal error: Lost conversation service")
        else:
            # Create new capture session
            capture_file = CaptureFile(
                capture_directory=app_state.config.captures.capture_dir,
                capture_uuid=capture_uuid,
                device_type=device_type,
                timestamp=timestamp,
                file_extension=file_extension
            )
            app_state.capture_files_by_id[capture_uuid] = capture_file

            # ... and associated conversation detection service
            detection_service = ConversationDetectionService(
                config=app_state.config,
                capture_filepath=capture_file.filepath,
                capture_timestamp=capture_file.timestamp
            )
            app_state.conversation_detection_service_by_id[capture_uuid] = detection_service
        
        # Get uploaded data
        content = await file.read()
        
        # Append to file
        bytes_written = 0
        if write_wav_header:
            bytes_written = append_to_wav_file(filepath=capture_file.filepath, sample_bytes=content, sample_rate=16000)
        else:
            with open(file=capture_file.filepath, mode="ab") as fp:
                bytes_written = fp.write(content)
        logging.info(f"{capture_file.filepath}: {bytes_written} bytes appended")

        # Conversation processing task
        task = ProcessAudioChunkTask(
            capture_file=capture_file,
            detection_service=detection_service,
            audio_data=content,
            format=file_extension
        )
        app_state.task_queue.put(task)

        # Success
        return JSONResponse(content={"message": f"Audio processed"})

    except Exception as e:
        logging.error(f"Failed to upload chunk: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

    
@router.post("/capture/process_capture")
async def process_capture(request: Request, capture_uuid: Annotated[str, Form()], app_state: AppState = Depends(AppState.authenticate_request)):
    try:
        # Get capture file
        filepath = find_audio_filepath(audio_directory=app_state.config.captures.capture_dir, capture_uuid=capture_uuid)
        logger.info(f"Found file to process: {filepath}")
        capture_file: CaptureFile = CaptureFile.from_filepath(filepath=filepath)
        if capture_file is None:
            logger.error(f"Filepath does not conform to expected format and cannot be processed: {filepath}")
            raise HTTPException(status_code=500, detail="Internal error: File is incorrectly named on server")
        
        # Conversation detection service
        detection_service: ConversationDetectionService = app_state.conversation_detection_service_by_id.get(capture_uuid)
        if detection_service is None:
            logger.error(f"Internal error: No conversation detection service exists for capture_uuid={capture_uuid}")
            raise HTTPException(status_Code=500, detail="Internal error: Lost conversation service")
        
        # Finish the conversation extraction.
        # TODO: If the server dies in the middle of an upload or before /process_capture is called,
        # we will not be able to do this because the in-memory session data will have been lost. A
        # more robust way to handle all this would be to 1) on first chunk, see if any existing file
        # data exists and process it all up to the new chunk and 2) on /process_capture, delete 
        # everything associated with the capture, remove everything from DB, and then regenerate 
        # everything. It is a brute force solution but conceptually simple and should be reasonably
        # robust.
        # Conversation processing task
        task = ProcessAudioChunkTask(
            capture_file=capture_file,
            detection_service=detection_service,
            format=os.path.splitext(capture_file.filepath)[1].lstrip(".")
        )
        app_state.task_queue.put(task)

        # Remove from app state
        if capture_uuid in app_state.capture_files_by_id:
            del app_state.capture_files_by_id[capture_uuid]
        if capture_uuid in app_state.conversation_detection_service_by_id:
            del app_state.conversation_detection_service_by_id[capture_uuid]
        
        return JSONResponse(content={"message": "Conversation processed"})
    except Exception as e:
        logger.error(f"Failed to process: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/capture/location")
async def receive_location(location: Location, db: Session = Depends(AppState.get_db), app_state: AppState = Depends(AppState.authenticate_request)):
    try:
        logger.info(f"Received location: {location}")
        new_location = create_location(db, location)
        return {"message": "Location received", "location_id": new_location.id}
    except Exception as e:
        logger.error(f"Error processing location: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))