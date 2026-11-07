window.TIMELINE = {
 "scenes": [
  {
   "id": "title",
   "start": 0.0,
   "dur": 12.065,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.237,
     "text": "Meet MeetingScribe."
    },
    {
     "t": 2.117,
     "dur": 5.611,
     "text": "It turns your Teams, Zoom and Google Meet calls into clean notes, and now into Jira tickets."
    },
    {
     "t": 8.008,
     "dur": 3.157,
     "text": "Without anything leaving your computer, unless you say so."
    }
   ]
  },
  {
   "id": "problem",
   "start": 12.065,
   "dur": 8,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.301,
     "text": "Meetings create work."
    },
    {
     "t": 2.181,
     "dur": 4.736,
     "text": "But notes go unwritten, action items get lost, and the tickets never get raised."
    }
   ]
  },
  {
   "id": "detect",
   "start": 20.065,
   "dur": 13.196,
   "lines": [
    {
     "t": 0.6,
     "dur": 2.496,
     "text": "MeetingScribe runs quietly in the background."
    },
    {
     "t": 3.376,
     "dur": 5.547,
     "text": "When you join a call, it starts recording two tracks: your microphone, and the meeting's own audio."
    },
    {
     "t": 9.203,
     "dur": 3.093,
     "text": "No buttons to press, and no bot joining the meeting."
    }
   ]
  },
  {
   "id": "apps",
   "start": 33.261,
   "dur": 11.849,
   "lines": [
    {
     "t": 0.6,
     "dur": 5.397,
     "text": "It works with the meetings you already have: the Teams and Zoom apps, and Google Meet in your browser."
    },
    {
     "t": 6.277,
     "dur": 4.672,
     "text": "Teams and Zoom on the web, Webex, Jitsi and Whereby work in the browser too."
    }
   ]
  },
  {
   "id": "browser",
   "start": 45.111,
   "dur": 17.676,
   "lines": [
    {
     "t": 0.6,
     "dur": 6.272,
     "text": "In a browser, it waits for two signals: the browser is using the microphone, and a meeting tab is open."
    },
    {
     "t": 7.152,
     "dur": 4.309,
     "text": "Then it records only the browser's sound, and names the note after the meeting."
    },
    {
     "t": 11.741,
     "dur": 5.035,
     "text": "Dictating a document, or recording a voice message, is never mistaken for a call."
    }
   ]
  },
  {
   "id": "pipeline",
   "start": 62.787,
   "dur": 17.017,
   "lines": [
    {
     "t": 0.6,
     "dur": 3.179,
     "text": "When the call ends, everything runs right on your computer."
    },
    {
     "t": 4.059,
     "dur": 1.899,
     "text": "Whisper transcribes both tracks."
    },
    {
     "t": 6.237,
     "dur": 4.309,
     "text": "Voiceprints tell the speakers apart, and recognise people it has heard before."
    },
    {
     "t": 10.827,
     "dur": 5.291,
     "text": "A local language model drops the small talk, then writes the summary and the action items."
    }
   ]
  },
  {
   "id": "note",
   "start": 79.804,
   "dur": 11,
   "lines": [
    {
     "t": 0.6,
     "dur": 3.904,
     "text": "A few minutes later, a Markdown note lands in your Meeting Notes folder."
    },
    {
     "t": 4.784,
     "dur": 2.816,
     "text": "Who was there, what was decided, and who owes what."
    }
   ]
  },
  {
   "id": "voices",
   "start": 90.804,
   "dur": 8.5,
   "lines": [
    {
     "t": 0.6,
     "dur": 2.112,
     "text": "An unknown voice? Name it once."
    },
    {
     "t": 2.992,
     "dur": 3.285,
     "text": "From the next meeting on, it is recognised automatically."
    }
   ]
  },
  {
   "id": "jira",
   "start": 99.304,
   "dur": 8.329,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.365,
     "text": "And now, the new part."
    },
    {
     "t": 2.245,
     "dur": 5.184,
     "text": "MeetingScribe can turn those action items into Jira tickets, with you approving every one."
    }
   ]
  },
  {
   "id": "signin",
   "start": 107.633,
   "dur": 17.631,
   "lines": [
    {
     "t": 0.6,
     "dur": 9.301,
     "text": "Run control jira, and sign in with a personal access token, an API token, or your browser's session cookie for single sign-on sites."
    },
    {
     "t": 10.181,
     "dur": 6.549,
     "text": "It can stay in your Mac's Keychain or in Windows Credential Manager, and you are asked again when it expires."
    }
   ]
  },
  {
   "id": "space",
   "start": 125.264,
   "dur": 6,
   "lines": [
    {
     "t": 0.6,
     "dur": 2.88,
     "text": "Then tell it which Jira space new tickets should go to."
    }
   ]
  },
  {
   "id": "extract",
   "start": 131.264,
   "dur": 9.417,
   "lines": [
    {
     "t": 0.6,
     "dur": 4.864,
     "text": "The same local model reads the meeting, and pulls out the work that belongs in Jira."
    },
    {
     "t": 5.744,
     "dur": 2.773,
     "text": "Owners, due dates, and the keywords to search for."
    }
   ]
  },
  {
   "id": "search",
   "start": 140.681,
   "dur": 12,
   "lines": [
    {
     "t": 0.6,
     "dur": 3.755,
     "text": "Then it searches every Jira space you can see, not just your own."
    },
    {
     "t": 4.635,
     "dur": 6.144,
     "text": "Each ticket it finds is compared with the new work: the same deliverable, merely related, or unrelated."
    }
   ]
  },
  {
   "id": "decide",
   "start": 152.681,
   "dur": 15,
   "lines": [
    {
     "t": 0.6,
     "dur": 3.989,
     "text": "Work that already has an open ticket gets a comment, not a duplicate."
    },
    {
     "t": 4.869,
     "dur": 3.477,
     "text": "Genuinely new work gets a new ticket, linked to its relatives."
    },
    {
     "t": 8.627,
     "dur": 4.843,
     "text": "And if a closed ticket's problem is back, it opens a fresh one, and links the history."
    }
   ]
  },
  {
   "id": "draft",
   "start": 167.681,
   "dur": 16.375,
   "lines": [
    {
     "t": 0.6,
     "dur": 5.397,
     "text": "Every field is filled in from the conversation, and checked against what your Jira space allows."
    },
    {
     "t": 6.277,
     "dur": 4.907,
     "text": "Type, priority, assignee, due date, components, even required custom fields."
    },
    {
     "t": 11.464,
     "dur": 4.011,
     "text": "Nothing is pushed until you say yes, and you can edit any field first."
    }
   ]
  },
  {
   "id": "board",
   "start": 184.056,
   "dur": 8.5,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.941,
     "text": "Say yes, and it is on the board."
    },
    {
     "t": 2.821,
     "dur": 3.605,
     "text": "The comments are posted, and the meeting note links to every ticket."
    }
   ]
  },
  {
   "id": "usecases",
   "start": 192.556,
   "dur": 13,
   "lines": [
    {
     "t": 0.6,
     "dur": 5.675,
     "text": "Use it for stand-ups, migration syncs, incident reviews, sprint planning, and stakeholder calls."
    },
    {
     "t": 6.555,
     "dur": 1.707,
     "text": "Anywhere talk turns into work."
    }
   ]
  },
  {
   "id": "privacy",
   "start": 205.556,
   "dur": 10.825,
   "lines": [
    {
     "t": 0.6,
     "dur": 3.712,
     "text": "Recording, transcription, and every model stay on your computer."
    },
    {
     "t": 4.592,
     "dur": 5.333,
     "text": "The only thing that leaves is the Jira update you approved, sent straight to your own Jira site."
    }
   ]
  },
  {
   "id": "windows",
   "start": 216.381,
   "dur": 13.407,
   "lines": [
    {
     "t": 0.6,
     "dur": 4.096,
     "text": "MeetingScribe runs on Apple Silicon Macs and on Windows PCs."
    },
    {
     "t": 4.976,
     "dur": 7.531,
     "text": "Both install with one command. On Windows it runs on the processor, or on an NVIDIA graphics card if you have one."
    }
   ]
  },
  {
   "id": "outro",
   "start": 229.788,
   "dur": 9.844,
   "lines": [
    {
     "t": 0.6,
     "dur": 4.843,
     "text": "Install it once, join your meetings as usual, and review the tickets when you are ready."
    },
    {
     "t": 5.723,
     "dur": 3.221,
     "text": "MeetingScribe. Meetings, written down, and followed up."
    }
   ]
  }
 ],
 "total": 239.632
};
