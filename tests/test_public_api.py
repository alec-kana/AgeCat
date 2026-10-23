import importlib.resources
import unittest

import agecat
from agecat import (
    AgeCatError,
    AgeCatService,
    AgeClient,
    DownloadError,
    DownloadOptions,
    DownloadProgressEvent,
    DownloadResult,
    Episode,
    EpisodeJob,
    FetchError,
    JobReport,
    ParseError,
)


class PublicApiTests(unittest.TestCase):
    def test_root_package_re_exports_stable_api(self):
        self.assertIs(agecat.AgeCatError, AgeCatError)
        self.assertIs(agecat.AgeCatService, AgeCatService)
        self.assertIs(agecat.AgeClient, AgeClient)
        self.assertIs(agecat.DownloadError, DownloadError)
        self.assertIs(agecat.DownloadOptions, DownloadOptions)
        self.assertIs(agecat.DownloadProgressEvent, DownloadProgressEvent)
        self.assertIs(agecat.DownloadResult, DownloadResult)
        self.assertIs(agecat.Episode, Episode)
        self.assertIs(agecat.EpisodeJob, EpisodeJob)
        self.assertIs(agecat.FetchError, FetchError)
        self.assertIs(agecat.JobReport, JobReport)
        self.assertIs(agecat.ParseError, ParseError)

    def test_package_declares_typed_marker(self):
        marker = importlib.resources.files("agecat").joinpath("py.typed")

        self.assertTrue(marker.is_file())


if __name__ == "__main__":
    unittest.main()
