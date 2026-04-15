def run_sender_loop() -> None:
    raise NotImplementedError(
        "Telegram outbox sender is not implemented. "
        "Outbox messages are currently delivered inline during process_job."
    )
