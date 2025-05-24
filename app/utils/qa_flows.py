# Define Q&A flows using dictionaries

qa_flows = {
    "main_menu": {
        "question": "Welcome to Groot! \nHow can I assist you today?\n"
                    "1. Visitor management\n"
                    "2. Bill payment\n"
                    "3. Electricity units purchase\n"
                    "\nPlease reply with a number.",
        "options": {
            "1": "Visitor management",
            "2": "Bill payment",
            "3": "Electricity units purchase"
        }
    },
    "bill_payment": {
        "question": "What bill would you like to pay?:\n"
                    "1. Facility levy\n"
                    "2. Estate party\n\n"
                    "Reply with a number for more info.",
        "options": {
            "1": "Facility levy",
            "2": "Estate party"
        }
    }

}
