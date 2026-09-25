from threads_platform.application.commands.conversations import ThreadsConversationHandler
from threads_platform.application.commands.discovery import ThreadsDiscoveryHandler
from threads_platform.application.commands.handlers import CommandHandler
from threads_platform.application.commands.publishing import ThreadsPublishingHandler
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.threads import ThreadsAccessTokenProvider, ThreadsAPI


def create_threads_command_handlers(
    api: ThreadsAPI,
    token_provider: ThreadsAccessTokenProvider,
    unit_of_work_factory: UnitOfWorkFactory,
) -> dict[str, CommandHandler]:
    publishing = ThreadsPublishingHandler(api, token_provider, unit_of_work_factory)
    conversations = ThreadsConversationHandler(api, token_provider, unit_of_work_factory)
    discovery = ThreadsDiscoveryHandler(api, token_provider, unit_of_work_factory)
    return {
        "threads.publish_text": publishing,
        "threads.publish_image": publishing,
        "threads.publish_video": publishing,
        "threads.publish_carousel": publishing,
        "threads.create_reply": publishing,
        "threads.sync_conversation": conversations,
        "threads.moderate_reply": conversations,
        "threads.discovery.create_campaign": discovery,
        "threads.discovery.complete_campaign": discovery,
        "threads.discovery.search": discovery,
        "threads.discovery.profile": discovery,
        "threads.discovery.mentions": discovery,
        "threads.discovery.conversation": discovery,
        "threads.discovery.resume": discovery,
        "threads.discovery.lead_status": discovery,
    }
