from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.video import capture_video_into_buffer
from label_inspection.smoke import SmokeExitCode, inspection_exit_code


class EmptyOrCorruptCapture:
    def read(self):
        return False, None


def test_empty_or_corrupt_video_is_not_a_passing_smoke():
    frames_read, frames_sampled = capture_video_into_buffer(
        EmptyOrCorruptCapture(),
        FrameBuffer(max_size=2),
        max_frames=5,
    )

    assert (frames_read, frames_sampled) == (0, 0)
    assert inspection_exit_code(None, frames_read=frames_read) == SmokeExitCode.FAILURE


def test_inspection_error_has_nonzero_deterministic_exit_code():
    assert inspection_exit_code("ERROR", frames_read=1) == SmokeExitCode.FAILURE
    assert inspection_exit_code("REVIEW", frames_read=1) == SmokeExitCode.OK
