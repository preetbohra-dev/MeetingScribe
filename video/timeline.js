window.TIMELINE = {
 "scenes": [
  {
   "id": "title",
   "start": 0.0,
   "dur": 10.657,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.259,
     "text": "Meet MeetingScribe."
    },
    {
     "t": 2.139,
     "dur": 4.459,
     "text": "It turns every Teams meeting into clean notes, and now into Jira tickets."
    },
    {
     "t": 6.877,
     "dur": 2.88,
     "text": "Without anything leaving your Mac, unless you say so."
    }
   ]
  },
  {
   "id": "problem",
   "start": 10.657,
   "dur": 8,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.323,
     "text": "Meetings create work."
    },
    {
     "t": 2.203,
     "dur": 4.736,
     "text": "But notes go unwritten, action items get lost, and the tickets never get raised."
    }
   ]
  },
  {
   "id": "detect",
   "start": 18.657,
   "dur": 13.516,
   "lines": [
    {
     "t": 0.6,
     "dur": 2.539,
     "text": "MeetingScribe runs quietly in the background."
    },
    {
     "t": 3.419,
     "dur": 5.824,
     "text": "When you join a Teams call, it starts recording two tracks: your microphone, and Teams' own audio."
    },
    {
     "t": 9.523,
     "dur": 3.093,
     "text": "No buttons to press, and no bot joining the meeting."
    }
   ]
  },
  {
   "id": "pipeline",
   "start": 32.173,
   "dur": 16.719,
   "lines": [
    {
     "t": 0.6,
     "dur": 2.859,
     "text": "When the call ends, everything runs right on the Mac."
    },
    {
     "t": 3.739,
     "dur": 1.899,
     "text": "Whisper transcribes both tracks."
    },
    {
     "t": 5.917,
     "dur": 4.331,
     "text": "Voiceprints tell the speakers apart, and recognise people it has heard before."
    },
    {
     "t": 10.528,
     "dur": 5.291,
     "text": "A local language model drops the small talk, then writes the summary and the action items."
    }
   ]
  },
  {
   "id": "note",
   "start": 48.892,
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
   "start": 59.892,
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
   "start": 68.392,
   "dur": 8.393,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.387,
     "text": "And now, the new part."
    },
    {
     "t": 2.267,
     "dur": 5.227,
     "text": "MeetingScribe can turn those action items into Jira tickets, with you approving every one."
    }
   ]
  },
  {
   "id": "signin",
   "start": 76.785,
   "dur": 15.497,
   "lines": [
    {
     "t": 0.6,
     "dur": 9.301,
     "text": "Run control jira, and sign in with a personal access token, an API token, or your browser's session cookie for single sign-on sites."
    },
    {
     "t": 10.181,
     "dur": 4.416,
     "text": "It can stay in your macOS Keychain, and you are asked again when it expires."
    }
   ]
  },
  {
   "id": "space",
   "start": 92.283,
   "dur": 6,
   "lines": [
    {
     "t": 0.6,
     "dur": 2.901,
     "text": "Then tell it which Jira space new tickets should go to."
    }
   ]
  },
  {
   "id": "extract",
   "start": 98.283,
   "dur": 9.503,
   "lines": [
    {
     "t": 0.6,
     "dur": 4.907,
     "text": "The same local model reads the meeting, and pulls out the work that belongs in Jira."
    },
    {
     "t": 5.787,
     "dur": 2.816,
     "text": "Owners, due dates, and the keywords to search for."
    }
   ]
  },
  {
   "id": "search",
   "start": 107.785,
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
   "start": 119.785,
   "dur": 15,
   "lines": [
    {
     "t": 0.6,
     "dur": 4.032,
     "text": "Work that already has an open ticket gets a comment, not a duplicate."
    },
    {
     "t": 4.912,
     "dur": 3.477,
     "text": "Genuinely new work gets a new ticket, linked to its relatives."
    },
    {
     "t": 8.669,
     "dur": 4.843,
     "text": "And if a closed ticket's problem is back, it opens a fresh one, and links the history."
    }
   ]
  },
  {
   "id": "draft",
   "start": 134.785,
   "dur": 16.396,
   "lines": [
    {
     "t": 0.6,
     "dur": 5.397,
     "text": "Every field is filled in from the conversation, and checked against what your Jira space allows."
    },
    {
     "t": 6.277,
     "dur": 4.928,
     "text": "Type, priority, assignee, due date, components, even required custom fields."
    },
    {
     "t": 11.485,
     "dur": 4.011,
     "text": "Nothing is pushed until you say yes, and you can edit any field first."
    }
   ]
  },
  {
   "id": "board",
   "start": 151.181,
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
   "start": 159.681,
   "dur": 13,
   "lines": [
    {
     "t": 0.6,
     "dur": 5.696,
     "text": "Use it for stand-ups, migration syncs, incident reviews, sprint planning, and stakeholder calls."
    },
    {
     "t": 6.576,
     "dur": 1.707,
     "text": "Anywhere talk turns into work."
    }
   ]
  },
  {
   "id": "privacy",
   "start": 172.681,
   "dur": 10.591,
   "lines": [
    {
     "t": 0.6,
     "dur": 3.456,
     "text": "Recording, transcription, and every model stay on your Mac."
    },
    {
     "t": 4.336,
     "dur": 5.355,
     "text": "The only thing that leaves is the Jira update you approved, sent straight to your own Jira site."
    }
   ]
  },
  {
   "id": "windows",
   "start": 183.272,
   "dur": 12.105,
   "lines": [
    {
     "t": 0.6,
     "dur": 4.693,
     "text": "MeetingScribe runs on Apple Silicon Macs today, and Windows support is coming."
    },
    {
     "t": 5.573,
     "dur": 5.632,
     "text": "First the Jira step, on any computer. Then recording and transcription on Windows PCs."
    }
   ]
  },
  {
   "id": "outro",
   "start": 195.377,
   "dur": 9.865,
   "lines": [
    {
     "t": 0.6,
     "dur": 4.843,
     "text": "Install it once, join your meetings as usual, and review the tickets when you are ready."
    },
    {
     "t": 5.723,
     "dur": 3.243,
     "text": "MeetingScribe. Meetings, written down, and followed up."
    }
   ]
  }
 ],
 "total": 205.243
};
