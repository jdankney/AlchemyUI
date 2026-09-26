"""The router: a message and its attachments in, a validated plan out.

schema.py   the closed vocabulary and plan validation
policy.py   plan words -> workflows, checkpoints and tuned numbers
prompt.py   what the model is told
client.py   one OpenAI-compatible backend (Llamacracy, or the local fallback)
router.py   primary -> repair -> fallback -> a plan made of the user's own words
"""
