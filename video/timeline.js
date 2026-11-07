window.TIMELINE = {
 "scenes": [
  {
   "id": "title",
   "start": 0.0,
   "dur": 10.615,
   "lines": [
    {
     "t": 0.6,
     "dur": 1.237,
     "text": "Meet MeetingScribe."
    },
    {
     "t": 2.117,
     "dur": 4.437,
     "text": "It turns every Teams meeting into clean notes, and now into Jira tickets."
    },
    {
     "t": 6.835,
     "dur": 2.88,
     "text": "Without anything leaving your Mac, unless you say so."
    }
   ]
  },
  {
   "id": "problem",
   "start": 10.615,
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
   "start": 18.615,
   "dur": 13.473,
   "lines": [
    {
     "t": 0.6,
     "dur": 2.496,
     "text": "MeetingScribe runs quietly in the background."
    },
    {
     "t": 3.376,
     "dur": 5.824,
     "text": "When you join a Teams call, it starts recording two tracks: your microphone, and Teams' own audio."
    },
    {
     "t": 9.48,
     "dur": 3.093,
     "text": "No buttons to press, and no bot joining the meeting."
    }
   ]
  },
  {
   "id": "pipeline",
   "start": 32.088,
   "dur": 16.697,
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
     "dur": 4.309,
     "text": "Voiceprints tell the speakers apart, and recognise people it has heard before."
    },
    {
     "t": 10.507,
     "dur": 5.291,
     "text": "A local language model drops the small talk, then writes the summary and the action items."
    }
   ]
  },
  {
   "id": "note",
   "start": 48.785,
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
   "start": 59.785,
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
   "start": 68.285,
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
   "start": 76.615,
   "dur": 15.455,
   "lines": [
    {
     "t": 0.6,
     "dur": 9.301,
     "text": "Run control jira, and sign in with a personal access token, an API token, or your browser's session cookie for single sign-on sites."
    },
    {
     "t": 10.181,
     "dur": 4.373,
     "text": "It can stay in your macOS Keychain, and you are asked again when it expires."
    }
   ]
  },
  {
   "id": "space",
   "start": 92.069,
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
   "start": 98.069,
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
   "start": 107.487,
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
   "start": 119.487,
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
   "start": 134.487,
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
   "start": 150.861,
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
   "start": 159.361,
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
   "start": 172.361,
   "dur": 10.569,
   "lines": [
    {
     "t": 0.6,
     "dur": 3.456,
     "text": "Recording, transcription, and every model stay on your Mac."
    },
    {
     "t": 4.336,
     "dur": 5.333,
     "text": "The only thing that leaves is the Jira update you approved, sent straight to your own Jira site."
    }
   ]
  },
  {
   "id": "windows",
   "start": 182.931,
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
   "start": 196.337,
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
 "total": 206.181
};
