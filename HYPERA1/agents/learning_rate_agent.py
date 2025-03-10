#!/usr/bin/env python
# Learning Rate Agent for Multi-Agent Hyperparameter Optimization

import numpy as np
import torch
from typing import Dict, List, Tuple, Optional, Union, Any
import logging
import os

from .base_agent import BaseHyperparameterAgent
from .shared_state import SharedStateManager

class LearningRateAgent(BaseHyperparameterAgent):
    """
    Specialized agent for optimizing the learning rate hyperparameter.
    
    This agent uses SAC to learn the optimal learning rate adjustment policy
    based on observed training metrics. It can dynamically adjust the learning
    rate during training to improve convergence and performance.
    """
    
    def __init__(
        self,
        shared_state_manager: SharedStateManager,
        initial_lr: float = 1e-3,
        min_lr: float = 1e-6,
        max_lr: float = 1e-1,
        update_frequency: int = 1,
        patience: int = 3,
        cooldown: int = 5,
        log_dir: str = "results",
        verbose: bool = True,
        metrics_to_track: List[str] = ["loss", "val_loss", "dice_score"],
        state_dim: int = 20,
        hidden_dim: int = 256,
        use_enhanced_state: bool = True,
        eligibility_trace_length: int = 10,
        n_step: int = 3,
        stability_weight: float = 0.3,
        generalization_weight: float = 0.4,
        efficiency_weight: float = 0.3,
        use_adaptive_scaling: bool = True,
        use_phase_aware_scaling: bool = True,
        auto_balance_components: bool = True,
        reward_clip_range: Tuple[float, float] = (-10.0, 10.0),
        reward_scaling_window: int = 100,
        device: str = "cuda" if torch.cuda.is_available() else "cpu",
        name: str = "learning_rate_agent",
        priority: int = 0
    ):
        """
        Initialize the learning rate agent.
        
        Args:
            shared_state_manager: Manager for shared state between agents
            initial_lr: Initial learning rate
            min_lr: Minimum learning rate
            max_lr: Maximum learning rate
            update_frequency: How often to update the learning rate (in epochs)
            patience: Epochs to wait before considering action
            cooldown: Epochs to wait after an action
            log_dir: Directory for saving logs and agent states
            verbose: Whether to print verbose output
            metrics_to_track: List of metrics to track
            state_dim: Dimension of state space
            hidden_dim: Dimension of hidden layers
            use_enhanced_state: Whether to use enhanced state representation
            eligibility_trace_length: Length of eligibility traces
            n_step: Number of steps for n-step returns
            stability_weight: Weight for stability component of reward
            generalization_weight: Weight for generalization component of reward
            efficiency_weight: Weight for efficiency component of reward
            use_adaptive_scaling: Whether to use adaptive reward scaling
            use_phase_aware_scaling: Whether to use phase-aware scaling
            auto_balance_components: Whether to auto-balance reward components
            reward_clip_range: Range for clipping rewards
            reward_scaling_window: Window size for reward statistics
            device: Device to use for training
            name: Name of agent
            priority: Priority of the agent (higher means more important)
        """
        super().__init__(
            name=name,
            hyperparameter_key="learning_rate",
            shared_state_manager=shared_state_manager,
            state_dim=state_dim,
            action_dim=1,
            action_space=(-1.0, 1.0),
            hidden_dim=hidden_dim,
            update_frequency=update_frequency,
            patience=patience,
            cooldown=cooldown,
            log_dir=log_dir,
            verbose=verbose,
            device=device,
            eligibility_trace_length=eligibility_trace_length,
            n_step=n_step,
            stability_weight=stability_weight,
            generalization_weight=generalization_weight,
            efficiency_weight=efficiency_weight,
            use_adaptive_scaling=use_adaptive_scaling,
            use_phase_aware_scaling=use_phase_aware_scaling,
            auto_balance_components=auto_balance_components,
            reward_clip_range=reward_clip_range,
            reward_scaling_window=reward_scaling_window,
            priority=priority
        )
        
        # Learning rate specific parameters
        self.min_lr = min_lr
        self.max_lr = max_lr
        self.metrics_to_track = metrics_to_track
        self.use_enhanced_state = use_enhanced_state
        
        # Initialize learning rate
        self.current_lr = initial_lr
        self.shared_state_manager.set_hyperparameter(self.hyperparameter_key, self.current_lr)
        
        # Add epochs_since_update attribute
        self.epochs_since_update = 0
        
        # Log initialization
        logging.info(f"Initialized with learning_rate={self.current_lr}")
    
    def get_state_representation(self) -> np.ndarray:
        """
        Get the current state representation for the agent.
        
        The state includes:
        - Enhanced metrics features (if enabled)
        - Recent history of tracked metrics
        - Current learning rate (normalized)
        - Epochs since last update
        - Training progress (current epoch / total epochs)
        - Overfitting signals
        
        Returns:
            State representation as numpy array
        """
        state_components = []
        
        # Use enhanced state representation if available and enabled
        if self.use_enhanced_state:
            # Get enhanced state features from the metric processor
            enhanced_features = self.shared_state_manager.get_enhanced_state_vector(self.metrics_to_track)
            
            if len(enhanced_features) > 0:
                state_components.extend(enhanced_features)
                
                # Add overfitting signals
                overfitting_signals = self.shared_state_manager.get_overfitting_signals()
                if overfitting_signals:
                    state_components.extend(list(overfitting_signals.values()))
                
                # Add normalized current learning rate
                normalized_lr = (np.log10(self.current_lr) - np.log10(self.min_lr)) / (np.log10(self.max_lr) - np.log10(self.min_lr))
                state_components.append(normalized_lr)
                
                # Add epochs since last update
                state_components.append(self.epochs_since_update / 10.0)  # Normalize
                
                # Add training progress
                current_epoch = self.shared_state_manager.get_current_epoch()
                total_epochs = self.shared_state_manager.total_epochs
                if total_epochs is not None and total_epochs > 0:
                    progress = current_epoch / total_epochs
                else:
                    progress = 0.0
                state_components.append(progress)
                
                # Ensure state has correct dimension
                state = np.array(state_components, dtype=np.float32)
                
                # Pad or truncate to match state_dim
                if len(state) < self.state_dim:
                    state = np.pad(state, (0, self.state_dim - len(state)), 'constant')
                elif len(state) > self.state_dim:
                    state = state[:self.state_dim]
                
                return state
        
        # Fall back to standard state representation if enhanced state is not available or disabled
        # Get metrics history
        metrics_history = {}
        for metric in self.metrics_to_track:
            metrics_history[metric] = self.shared_state_manager.get_metric_history(
                metric, window_size=self.state_dim // len(self.metrics_to_track)
            )
        
        # Calculate metrics statistics
        
        # Add recent metric values
        for metric in self.metrics_to_track:
            history = metrics_history[metric]
            if len(history) > 0:
                # Add latest value
                state_components.append(history[-1])
                
                # Add trend (difference between latest and oldest in window)
                if len(history) > 1:
                    state_components.append(history[-1] - history[0])
                else:
                    state_components.append(0.0)
                
                # Add volatility (standard deviation)
                if len(history) > 1:
                    state_components.append(np.std(history))
                else:
                    state_components.append(0.0)
            else:
                # If no history, pad with zeros
                state_components.extend([0.0, 0.0, 0.0])
        
        # Add normalized current learning rate
        normalized_lr = (np.log10(self.current_lr) - np.log10(self.min_lr)) / (np.log10(self.max_lr) - np.log10(self.min_lr))
        state_components.append(normalized_lr)
        
        # Add epochs since last update
        state_components.append(self.epochs_since_update / 10.0)  # Normalize
        
        # Add training progress
        current_epoch = self.shared_state_manager.get_current_epoch()
        total_epochs = self.shared_state_manager.total_epochs
        if total_epochs is not None and total_epochs > 0:
            progress = current_epoch / total_epochs
        else:
            progress = 0.0
        state_components.append(progress)
        
        # Ensure state has correct dimension
        state = np.array(state_components, dtype=np.float32)
        
        # Pad or truncate to match state_dim
        if len(state) < self.state_dim:
            state = np.pad(state, (0, self.state_dim - len(state)), 'constant')
        elif len(state) > self.state_dim:
            state = state[:self.state_dim]
        
        return state
    
    def action_to_hyperparameter(self, action: float) -> float:
        """
        Convert normalized action to actual learning rate value.
        
        Args:
            action: Normalized action from SAC (-1.0 to 1.0)
            
        Returns:
            New learning rate value
        """
        # Convert action to multiplicative factor
        # Action -1.0 -> divide by 3
        # Action 0.0 -> no change
        # Action 1.0 -> multiply by 3
        factor = 3.0 ** action
        
        # Apply factor to current learning rate
        new_lr = self.current_lr * factor
        
        # Clip to valid range
        new_lr = np.clip(new_lr, self.min_lr, self.max_lr)
        
        return new_lr
    
    def update_hyperparameter(self, action: float) -> Dict[str, Any]:
        """
        Update the learning rate based on the agent's action.
        
        Args:
            action: Normalized action from SAC (-1.0 to 1.0)
            
        Returns:
            Dictionary with update information
        """
        # Convert action to learning rate
        new_lr = self.action_to_hyperparameter(action)
        
        # Calculate relative change
        relative_change = new_lr / self.current_lr
        
        # Update current value
        old_lr = self.current_lr
        self.current_lr = new_lr
        
        # Update shared state
        self.shared_state_manager.set_hyperparameter(self.hyperparameter_key, self.current_lr)
        
        # Log update
        logging.info(f"Updated learning_rate: {old_lr:.6f} -> {self.current_lr:.6f} (factor: {relative_change:.2f})")
        
        # Return update info
        return {
            "old_value": old_lr,
            "new_value": self.current_lr,
            "relative_change": relative_change,
            "hyperparameter": self.hyperparameter_key
        }
    
    def _process_action(self, action):
        """
        Process the action from SAC to get the new learning rate.
        
        Args:
            action: Action from SAC
            
        Returns:
            New learning rate value
        """
        # Handle both scalar and array/list actions
        if isinstance(action, (list, np.ndarray)):
            return self.action_to_hyperparameter(action[0])
        else:
            return self.action_to_hyperparameter(action)
    
    def _apply_action(self, new_lr: float) -> None:
        """
        Apply the new learning rate to the optimizer.
        
        Args:
            new_lr: New learning rate value
        """
        self.current_lr = new_lr
        self.shared_state_manager.set_hyperparameter(self.hyperparameter_key, new_lr)
        
        if self.verbose:
            logging.info(f"Learning rate updated to {new_lr:.6f}")
    
    def select_action(self, epoch: int) -> Optional[float]:
        """
        Select an action based on the current state.
        
        Args:
            epoch: Current training epoch
            
        Returns:
            Action value or None if no action should be taken
        """
        if not self.should_update(epoch):
            return None
            
        state = self.get_state_representation()
        action = self.sac.select_action(state)
        processed_action = self._process_action(action)
        
        return processed_action
    
    def _get_state_representation(self) -> np.ndarray:
        """
        Get the current state representation for the agent.
        
        Returns:
            State representation as numpy array
        """
        return self.get_state_representation()
