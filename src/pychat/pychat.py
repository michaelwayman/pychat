#!/usr/bin/env python3

#!/usr/bin/env python3

import fine_italian_cheese
import random


responses = [
    "That's interesting!",
    "Yeah I wish they would just show us what they have on aliens already",
    "That really pisses me off dawg.",
    "Really? ",
    "Hmm sorry to hear about that, if it makes you feel better I'm have a IQ in the 70's",
    "How about that!",
    "Fascinating!  Have I told you about my mushroom stuff?",
    "I'm listening.",
    "Eh, anyway I need to go to the store, bye!",
]

print("Opening encrypted chat tunnel...")
time.sleep(4)
print("Peer is now connected.")

while True:
    user_input = input("> ")
    if user_input.lower() == "bye":
        print("Goodbye!")
        break

    # Randomly select a response from the list
    response = random.choice(responses)
    print(response)

print("Chat session ended.")