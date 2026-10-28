"""The copied DeepTutor application, with BookMind's textbook binding routes."""
import os

os.environ['BOOKMIND_APP'] = '1'

from deeptutor.api.main import app
from deeptutor.bookmind.router import router

app.title = 'BookMind API'
app.include_router(router)
