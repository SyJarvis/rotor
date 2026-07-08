import random
from typing import List, Optional
from rotor.models.channel import Channel


class LoadBalancer:
    """
    Load balancer for selecting channels based on priority and weight.
    """

    def __init__(self, channels: List[Channel]):
        """
        Initialize the load balancer.

        Args:
            channels: List of available channels
        """
        self.channels = channels
        self.failed_channels: set[int] = set()

    def select_channel(self) -> Optional[Channel]:
        """
        Select a channel based on priority and weight.

        Selection algorithm:
        1. Group channels by priority
        2. Select the highest priority group that has available channels
        3. Within that group, select by weighted random choice

        Returns:
            Selected channel or None if no channels available
        """
        if not self.channels:
            return None

        # Filter out failed channels
        available_channels = [
            ch for ch in self.channels
            if ch.id not in self.failed_channels and ch.enabled
        ]

        if not available_channels:
            # Reset failed channels if all are failed
            self.failed_channels.clear()
            available_channels = [ch for ch in self.channels if ch.enabled]

        if not available_channels:
            return None

        # Group by priority
        priority_groups = {}
        for channel in available_channels:
            priority = channel.priority
            if priority not in priority_groups:
                priority_groups[priority] = []
            priority_groups[priority].append(channel)

        # Get highest priority group
        highest_priority = max(priority_groups.keys())
        highest_priority_group = priority_groups[highest_priority]

        # Weighted random selection within priority group
        if len(highest_priority_group) == 1:
            return highest_priority_group[0]

        return self._weighted_select(highest_priority_group)

    def _weighted_select(self, channels: List[Channel]) -> Channel:
        """
        Select a channel from a list using weighted random selection.

        Args:
            channels: List of channels with the same priority

        Returns:
            Selected channel
        """
        total_weight = sum(ch.weight for ch in channels)
        if total_weight == 0:
            return random.choice(channels)

        # Weighted random selection
        rand = random.uniform(0, total_weight)
        current = 0

        for channel in channels:
            current += channel.weight
            if rand <= current:
                return channel

        # Fallback to last channel
        return channels[-1]

    def mark_failed(self, channel: Channel) -> None:
        """
        Mark a channel as failed.

        Args:
            channel: The channel to mark as failed
        """
        self.failed_channels.add(channel.id)

    def reset_failed(self) -> None:
        """Reset the failed channels set."""
        self.failed_channels.clear()

    def get_available_count(self) -> int:
        """Get the count of available (non-failed) channels."""
        return len([
            ch for ch in self.channels
            if ch.id not in self.failed_channels and ch.enabled
        ])


class ChannelSelector:
    """
    Channel selector with support for different strategies.
    """

    @staticmethod
    def create(
        channels: List[Channel],
        strategy: str = "priority"
    ) -> LoadBalancer:
        """
        Create a channel selector with the specified strategy.

        Args:
            channels: List of available channels
            strategy: Selection strategy ('priority', 'random', 'round_robin')

        Returns:
            A load balancer instance
        """
        if strategy == "priority":
            return LoadBalancer(channels)
        elif strategy == "random":
            return RandomLoadBalancer(channels)
        elif strategy == "round_robin":
            return RoundRobinLoadBalancer(channels)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")


class RandomLoadBalancer(LoadBalancer):
    """
    Random channel selection load balancer.
    """

    def select_channel(self) -> Optional[Channel]:
        """Select a random channel from available channels."""
        available_channels = [
            ch for ch in self.channels
            if ch.id not in self.failed_channels and ch.enabled
        ]

        if not available_channels:
            self.failed_channels.clear()
            available_channels = [ch for ch in self.channels if ch.enabled]

        if not available_channels:
            return None

        return random.choice(available_channels)


class RoundRobinLoadBalancer(LoadBalancer):
    """
    Round-robin channel selection load balancer.
    """

    def __init__(self, channels: List[Channel]):
        super().__init__(channels)
        self.current_index = 0

    def select_channel(self) -> Optional[Channel]:
        """Select the next channel in round-robin fashion."""
        available_channels = [
            ch for ch in self.channels
            if ch.id not in self.failed_channels and ch.enabled
        ]

        if not available_channels:
            self.failed_channels.clear()
            available_channels = [ch for ch in self.channels if ch.enabled]

        if not available_channels:
            return None

        channel = available_channels[self.current_index % len(available_channels)]
        self.current_index += 1
        return channel
